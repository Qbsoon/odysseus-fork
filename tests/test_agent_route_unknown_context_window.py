"""Regression: an UNKNOWN model context window must not be budgeted as a small
known server, and must get the same DEFAULT_CONTEXT budget the plain chat path
already assumes.

OpenCode Go's /models catalog reports no context-window fields, so
``budget_context_for_model`` returns 0 for its models. The agent route budget
used to treat ``context <= 8192`` as "small local server" and clamped the
input budget to 1200 tokens; ``trim_for_context`` then hit its last-ditch
branch and sent the provider only the latest user message — every earlier
turn of the conversation was silently dropped (OpenCode Go history loss,
2026-10-10). Unknown windows are now substituted with DEFAULT_CONTEXT before
budgeting, matching the plain chat path.
"""

import asyncio
import pytest

import src.agent_loop as al
from src.context_budget import DEFAULT_BUDGET
from src.model_context import _lookup_known
from src.tool_capabilities import ToolGateDecision


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _patch_common(monkeypatch):
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    import src.context_compactor as context_compactor
    monkeypatch.setattr(
        context_compactor, "get_context_length", lambda *a, **k: 128_000
    )
    monkeypatch.setattr(
        al, "tool_result_should_arm_gate", lambda *a, **k: False, raising=False
    )
    monkeypatch.setattr(
        al.ToolRunSecurityContext,
        "decision_for",
        lambda self, *a, **k: ToolGateDecision(True),
        raising=False,
    )


def _patch_route_budget(monkeypatch, context, budget):
    """Pin the route's discovered window and the configured input budget."""
    import src.settings as settings

    real_get_setting = settings.get_setting

    def fake_get_setting(key, default=None):
        if key == "agent_input_token_budget":
            return budget
        return real_get_setting(key, default)

    monkeypatch.setattr(al, "get_setting", fake_get_setting, raising=False)
    monkeypatch.setattr(
        "src.model_context.budget_context_for_model", lambda *a, **k: context
    )


def _history(turns):
    messages = []
    for i in range(1, turns + 1):
        messages.append({"role": "user", "content": f"unique-early-turn-{i}"})
        messages.append({"role": "assistant", "content": f"unique-early-reply-{i}"})
    messages.append({"role": "user", "content": "unique-final-turn"})
    return messages


def _outgoing_text(messages):
    parts = []
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _capture_provider(monkeypatch, captured):
    async def _fake_stream(_candidates, messages, **kwargs):
        captured["messages"] = messages
        yield 'data: {"delta":"done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(
        al, "stream_llm_with_fallback", _fake_stream, raising=False
    )


@pytest.mark.asyncio
async def test_unknown_window_gets_default_context_budget(monkeypatch):
    """context=0 (unproven window) is budgeted against DEFAULT_CONTEXT, so the
    auto (non-explicit) budget scales to 0.85 * 128K instead of collapsing to
    the conservative 6000-token default that dropped older history."""
    _patch_common(monkeypatch)
    _patch_route_budget(monkeypatch, context=0, budget=DEFAULT_BUDGET)
    captured = {}
    _capture_provider(monkeypatch, captured)

    _collect(al.stream_agent_loop(
        "http://x/v1", "m", _history(40), max_rounds=1, relevant_tools=set(),
    ))

    text = _outgoing_text(captured["messages"])
    assert "unique-early-turn-1" in text
    assert "unique-final-turn" in text


@pytest.mark.asyncio
async def test_runtime_declared_window_beats_unknown_default(monkeypatch):
    """A client-declared transport window still caps an unknown model — the
    DEFAULT_CONTEXT substitution must not override it."""
    _patch_common(monkeypatch)
    _patch_route_budget(monkeypatch, context=0, budget=DEFAULT_BUDGET)
    captured = {}
    _capture_provider(monkeypatch, captured)

    _collect(al.stream_agent_loop(
        "http://x/v1", "m", _history(40), max_rounds=1, relevant_tools=set(),
        client_runtime_context={"model_context_window": 4096},
    ))

    text = _outgoing_text(captured["messages"])
    # A 4K declared window cannot hold 40 turns; aggressive trimming is
    # expected and the declaration must win over the 128K default.
    assert "unique-early-turn-1" not in text
    assert "unique-final-turn" in text


@pytest.mark.asyncio
async def test_unknown_window_keeps_earlier_turns(monkeypatch):
    """context=0 (unproven window) must keep the auto budget, not clamp to 1200."""
    _patch_common(monkeypatch)
    _patch_route_budget(monkeypatch, context=0, budget=60000)
    captured = {}
    _capture_provider(monkeypatch, captured)

    _collect(al.stream_agent_loop(
        "http://x/v1", "m", _history(6), max_rounds=1, relevant_tools=set(),
    ))

    text = _outgoing_text(captured["messages"])
    assert "unique-early-turn-1" in text
    assert "unique-final-turn" in text


@pytest.mark.asyncio
async def test_small_known_window_still_clamps(monkeypatch):
    """The small-server headroom clamp must keep applying to KNOWN small windows."""
    _patch_common(monkeypatch)
    _patch_route_budget(monkeypatch, context=4096, budget=60000)
    captured = {}
    _capture_provider(monkeypatch, captured)

    _collect(al.stream_agent_loop(
        "http://x/v1", "m", _history(40), max_rounds=1, relevant_tools=set(),
    ))

    text = _outgoing_text(captured["messages"])
    # A 4K window cannot hold 40 turns; aggressive trimming is expected here.
    assert "unique-early-turn-1" not in text
    assert "unique-final-turn" in text


def test_opencode_go_models_resolve_known_windows():
    """Go's catalog reports no windows; the known table must fill the gap."""
    assert _lookup_known("glm-5.3-flash") == 128000
    assert _lookup_known("glm-5.3") == 128000
    assert _lookup_known("glm-5.2") == 128000
    assert _lookup_known("qwen3.7-plus") == 256000
    assert _lookup_known("grok-4.7") == 200000
    assert _lookup_known("grok-4.6") == 200000
    assert _lookup_known("gpt-6-luna") == 272000
    assert _lookup_known("gpt-5.6-luna") == 272000
    assert _lookup_known("claude-haiku-5-5") == 100000
    assert _lookup_known("mimo-v2.6-flash") == 1048576
