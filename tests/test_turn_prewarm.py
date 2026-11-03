"""Turn prewarming (Task 2 follow-up): message-independent inputs for the
next chat turn are prepared at session creation / model switch, and the
compaction check runs post-response instead of blocking the next send.
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.turn_prewarm as turn_prewarm


ROOT = Path(__file__).resolve().parents[1]


def _fake_sess(session_id="sess-1", messages=None):
    sess = SimpleNamespace(
        id=session_id,
        endpoint_url="https://opencode.ai/zen/go/v1/chat/completions",
        model="glm-5.3-flash",
        headers={},
        messages=([{"role": "user", "content": "hi"}] if messages is None else list(messages)),
    )
    sess.get_context_messages = lambda: list(sess.messages)
    return sess


@pytest.mark.asyncio
async def test_prewarm_warms_base_prompt_cache(monkeypatch):
    import src.agent_loop as al

    monkeypatch.setattr(al, "set_active_model", lambda model: None)
    monkeypatch.setattr(al, "get_builtin_overrides", lambda: {})
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(al, "_cached_base_prompt", None)
    monkeypatch.setattr(al, "_cached_base_prompt_key", None)
    monkeypatch.setattr("src.tool_security.blocked_tools_for_owner", lambda owner: set())

    await turn_prewarm.prewarm_session_turn(_fake_sess(), owner="admin")

    assert al._cached_base_prompt is not None
    cached = al._cached_base_prompt

    # A real turn with the same common shape must HIT the cache (the cached
    # object is reused, not rebuilt).
    al._build_system_prompt(
        [{"role": "user", "content": "real question"}],
        model="glm-5.3-flash",
        active_document=None,
        mcp_mgr=None,
        disabled_tools=set(),
        needs_admin=False,
        relevant_tools=None,
        owner="admin",
        endpoint_url="https://opencode.ai/zen/go/v1/chat/completions",
    )
    assert al._cached_base_prompt is cached


@pytest.mark.asyncio
async def test_prewarm_compaction_runs_maybe_compact_on_history(monkeypatch):
    import src.context_compactor as context_compactor

    calls = []

    async def fake_maybe_compact(sess, endpoint, model, messages, headers=None, owner=None, **kw):
        calls.append((endpoint, model, list(messages), owner))
        return messages, 128000, False

    monkeypatch.setattr(context_compactor, "maybe_compact", fake_maybe_compact)

    sess = _fake_sess(messages=[{"role": "user", "content": "a"}])
    await turn_prewarm.prewarm_compaction(sess, owner="admin")

    assert len(calls) == 1
    endpoint, model, messages, owner = calls[0]
    assert endpoint == sess.endpoint_url
    assert model == sess.model
    assert messages == sess.messages
    assert owner == "admin"


@pytest.mark.asyncio
async def test_prewarm_compaction_skips_empty_history(monkeypatch):
    import src.context_compactor as context_compactor

    async def fail_compact(*a, **kw):
        raise AssertionError("empty history must not reach maybe_compact")

    monkeypatch.setattr(context_compactor, "maybe_compact", fail_compact)

    await turn_prewarm.prewarm_compaction(_fake_sess(messages=[]), owner="admin")


@pytest.mark.asyncio
async def test_schedule_is_single_flight(monkeypatch):
    calls = []

    async def fake_prewarm(sess, owner=None):
        calls.append(1)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(turn_prewarm, "prewarm_session_turn", fake_prewarm)

    sess = _fake_sess()
    turn_prewarm.schedule_session_prewarm(sess, "admin")
    turn_prewarm.schedule_session_prewarm(sess, "admin")
    await asyncio.sleep(0.15)

    assert len(calls) == 1
    assert turn_prewarm._INFLIGHT == {}


def test_schedule_respects_env_disable(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_TURN_PREWARM", "0")
    assert turn_prewarm.turn_prewarm_enabled() is False

    async def fail_prewarm(sess, owner=None):
        raise AssertionError("prewarm must not run when disabled")

    monkeypatch.setattr(turn_prewarm, "prewarm_session_turn", fail_prewarm)
    turn_prewarm.schedule_session_prewarm(_fake_sess(), "admin")
    assert turn_prewarm._INFLIGHT == {}


def test_trigger_points_are_wired():
    sessions_src = (ROOT / "routes" / "session_routes.py").read_text()
    helpers_src = (ROOT / "routes" / "chat_helpers.py").read_text()

    # Session creation and the mid-session model switch both prewarm.
    assert sessions_src.count("schedule_session_prewarm(") == 2
    # Post-response: compaction pre-check joins the sequential queue and is
    # skipped for incognito/compare turns.
    assert "prewarm_compaction(sess, owner)" in helpers_src
    assert "if not incognito and not compare_mode:" in helpers_src


def test_schedule_from_sync_route_context_uses_captured_loop(monkeypatch):
    """Sync routes run in a threadpool with NO running loop — a bare
    asyncio.create_task there raises (and used to be silently swallowed,
    turning the session-creation prewarm into a no-op). Scheduling must go
    through the captured app loop instead."""
    import threading

    ran = threading.Event()

    async def fake_prewarm(sess, owner=None):
        ran.set()

    monkeypatch.setattr(turn_prewarm, "prewarm_session_turn", fake_prewarm)

    loop = asyncio.new_event_loop()
    turn_prewarm.set_main_loop(loop)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    try:
        thread.start()
        # Called from a plain sync context (no running loop anywhere).
        turn_prewarm.schedule_session_prewarm(_fake_sess(), "admin")
        assert ran.wait(timeout=5), "prewarm never ran on the captured loop"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        turn_prewarm.set_main_loop(None)
        turn_prewarm._INFLIGHT.clear()


async def test_wait_for_compaction_awaits_inflight_prewarm(monkeypatch):
    """A send that starts while the post-response compaction prewarm is
    mid-LLM-call must await it, not race it (double summarization)."""
    import src.context_compactor as context_compactor

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_compact(sess, endpoint, model, messages, headers=None, owner=None, **kw):
        started.set()
        await release.wait()
        return messages, 128000, False

    monkeypatch.setattr(context_compactor, "maybe_compact", slow_compact)

    sess = _fake_sess()
    prewarm_task = asyncio.create_task(
        turn_prewarm.prewarm_compaction(sess, owner="admin")
    )
    await started.wait()
    assert sess.id in turn_prewarm._COMPACT_INFLIGHT

    waiter = asyncio.create_task(turn_prewarm.wait_for_compaction(sess.id))
    await asyncio.sleep(0.01)
    assert not waiter.done(), "waiter must block while compaction is in flight"

    release.set()
    await prewarm_task
    await asyncio.wait_for(waiter, timeout=5)
    assert turn_prewarm._COMPACT_INFLIGHT == {}


async def test_wait_for_compaction_returns_when_idle():
    await asyncio.wait_for(
        turn_prewarm.wait_for_compaction("no-such-session"), timeout=5
    )


async def test_prewarm_compaction_clears_inflight_on_error(monkeypatch):
    import src.context_compactor as context_compactor

    async def failing_compact(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(context_compactor, "maybe_compact", failing_compact)

    sess = _fake_sess()
    with pytest.raises(RuntimeError):
        await turn_prewarm.prewarm_compaction(sess, owner="admin")

    assert turn_prewarm._COMPACT_INFLIGHT == {}
