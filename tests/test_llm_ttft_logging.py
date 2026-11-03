"""TTFT attribution logging for the generic OpenAI-compatible stream path.

Task 2 Phase 2A: every stream must log one ``[llm-ttft]`` line at INFO (no env
flag) with time-to-response-headers (``ttfb_ms``), time-to-first *meaningful*
SSE frame (``ttft_ms``) and a classification of that frame (thinking /
tool_calls / content / error), so a slow OpenCode Go first token can be
attributed without enabling debug flags.
"""
import asyncio
import json
import logging

import pytest

from src import llm_core


class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _sse(delta):
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


def _drive(monkeypatch, lines, model="glm-5.3"):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.delenv("ODYSSEUS_DEBUG_LLM_SHAPE", raising=False)

    async def run():
        chunks = []
        async for chunk in llm_core.stream_llm(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            model,
            [{"role": "user", "content": "hi"}],
            headers={"Authorization": "Bearer k"},
        ):
            chunks.append(chunk)
        return chunks

    return asyncio.run(run())


def _ttft_records(caplog):
    return [r for r in caplog.records if "[llm-ttft]" in r.getMessage()]


def test_ttft_logged_at_info_without_debug_flag(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="src.llm_core")
    _drive(monkeypatch, [_sse({"content": "hello"}), "data: [DONE]"])

    records = _ttft_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert "first=content" in message
    assert "ttfb_ms=" in message
    assert "ttft_ms=" in message
    assert "provider=" in message
    assert "model=glm-5.3" in message


def test_ttft_classifies_thinking_first_frame(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="src.llm_core")
    _drive(monkeypatch, [_sse({"reasoning_content": "pondering"}), "data: [DONE]"])

    records = _ttft_records(caplog)
    assert len(records) == 1
    assert "first=thinking" in records[0].getMessage()
    assert "delta_keys=['reasoning_content']" in records[0].getMessage()


def test_ttft_ignores_role_only_preamble(monkeypatch, caplog):
    # A role-only preamble frame is not meaningful output; the TTFT clock must
    # stop at the first substantive frame, and only one line must be logged.
    caplog.set_level(logging.INFO, logger="src.llm_core")
    _drive(monkeypatch, [
        _sse({"role": "assistant"}),
        _sse({"content": "real answer"}),
        "data: [DONE]",
    ])

    records = _ttft_records(caplog)
    assert len(records) == 1
    assert "first=content" in records[0].getMessage()


def test_ttft_classifies_tool_calls_first_frame(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="src.llm_core")
    _drive(monkeypatch, [
        _sse({"tool_calls": [{"id": "c1", "name": "web_fetch", "arguments": "{}"}]}),
        "data: [DONE]",
    ])

    records = _ttft_records(caplog)
    assert len(records) == 1
    assert "first=tool_calls" in records[0].getMessage()


def test_classify_first_stream_frame_kinds():
    frame = lambda delta: {"choices": [{"delta": delta}]}
    assert llm_core._classify_first_stream_frame({"error": {"message": "x"}}) == "error"
    assert llm_core._classify_first_stream_frame(frame({"reasoning": "r"})) == "thinking"
    assert llm_core._classify_first_stream_frame(frame({"thinking": "t"})) == "thinking"
    assert llm_core._classify_first_stream_frame(frame({"tool_calls": [{}]})) == "tool_calls"
    assert llm_core._classify_first_stream_frame(frame({"content": "hi"})) == "content"
    assert llm_core._classify_first_stream_frame(frame({"content": "   "})) == "other"
    assert llm_core._classify_first_stream_frame(frame({})) == "other"
