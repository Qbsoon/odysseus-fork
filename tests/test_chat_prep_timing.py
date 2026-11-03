"""Chat pre-flight stage timing (Task 2 Phase 2A).

``build_chat_context`` must record wall-clock stage timings (preprocess,
history_persist, preface + its memory/rag/web_search/url_fetch/skills
sub-stages, compaction, total), attach them to the returned ``ChatContext`` and
log them as ``[chat-prep-timing]`` so a slow time-to-first-token can be
attributed to the pre-flight. The stream path must surface them as a
``chat_prep`` SSE event.
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

import routes.chat_helpers as chat_helpers
from routes.chat_helpers import build_chat_context
from src.chat_processor import ChatProcessor


# --------------------------------------------------------------------------- #
# ChatProcessor.build_context_preface sub-stage timings
# --------------------------------------------------------------------------- #

class _FakeMemoryManager:
    def __init__(self, entries):
        self._entries = entries

    def load(self, owner=None):
        return list(self._entries)

    def increment_uses(self, ids):
        pass


def _processor(memory_entries=()):
    proc = ChatProcessor.__new__(ChatProcessor)
    proc.memory_manager = _FakeMemoryManager(list(memory_entries))
    proc.personal_docs_manager = SimpleNamespace(rag_manager=None)
    proc.memory_vector = None
    proc.skills_manager = None
    return proc


def test_preface_timings_record_executed_stages_only():
    proc = _processor(memory_entries=[
        {"id": "m1", "text": "the user keeps notes about project odysseus", "pinned": False},
    ])
    preface, rag_sources, web_sources = proc.build_context_preface(
        message="what does the project odysseus do?",
        session=SimpleNamespace(),
        use_memory=True,
        use_rag=False,
        use_web=False,
    )

    timings = proc._last_preface_timings
    assert "memory" in timings
    assert timings["memory"] >= 0.0
    # Skipped stages get no entry — absence means "did not run".
    assert "rag" not in timings
    assert "web_search" not in timings
    assert "url_fetch" not in timings
    assert "skills" not in timings
    assert preface  # the memory context message was actually injected


def test_preface_timings_reset_per_call():
    proc = _processor()
    proc.build_context_preface(
        message="hi", session=SimpleNamespace(),
        use_memory=False, use_rag=False, use_web=False,
    )
    assert proc._last_preface_timings == {}


# --------------------------------------------------------------------------- #
# build_chat_context attaches + logs the timings
# --------------------------------------------------------------------------- #

async def _build_context(monkeypatch, caplog, preface_timings=None):
    async def fake_preprocess(chat_handler, message, att_ids, sess, **kwargs):
        return chat_helpers.PreprocessedMessage(
            enhanced_message=message,
            user_content=message,
            text_for_context=message,
            youtube_transcripts=[],
            attachment_meta=[],
        )

    def fake_extract_preset(chat_handler, preset_id):
        return chat_helpers.PresetInfo(
            temperature=0.7, max_tokens=1024, system_prompt=None, character_name=None,
        )

    def fake_add_user_message(sess, chat_handler, preprocessed, incognito=False, **kwargs):
        sess.messages.append({"role": "user", "content": preprocessed.user_content})

    async def fake_maybe_compact(sess, endpoint_url, model, messages, headers, owner=None):
        return messages, 8192, False

    def fake_build_context_preface(**kwargs):
        # Mimic ChatProcessor recording its sub-stage timings.
        chat_processor._last_preface_timings = dict(preface_timings or {})
        return [], [], []

    monkeypatch.setattr(chat_helpers, "preprocess", fake_preprocess)
    monkeypatch.setattr(chat_helpers, "extract_preset", fake_extract_preset)
    monkeypatch.setattr(chat_helpers, "add_user_message", fake_add_user_message)
    monkeypatch.setattr(chat_helpers, "load_prefs_for_user", lambda user: {})
    monkeypatch.setattr(chat_helpers, "effective_user", lambda request: "tester")
    monkeypatch.setattr(chat_helpers, "_normalize_model_id_from_cache", lambda sess: None)
    monkeypatch.setattr(chat_helpers, "normalize_model_id", lambda endpoint_url, model, **kwargs: None)
    monkeypatch.setattr(chat_helpers, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(chat_helpers, "trim_for_context", lambda messages, context_length: messages)

    import src.user_time as user_time
    monkeypatch.setattr(
        user_time,
        "current_datetime_context_message",
        lambda now_utc=None: {"role": "user", "content": "[Context — current date/time]"},
        raising=False,
    )

    sess = SimpleNamespace(
        endpoint_url="http://model.local/v1",
        model="test-model",
        headers={},
        messages=[],
    )
    sess.get_context_messages = lambda: list(sess.messages)

    chat_processor = SimpleNamespace(build_context_preface=fake_build_context_preface)
    request = SimpleNamespace()

    caplog.set_level(logging.INFO, logger="routes.chat_helpers")
    ctx = await build_chat_context(
        sess=sess,
        request=request,
        chat_handler=SimpleNamespace(),
        chat_processor=chat_processor,
        message="hello",
        session_id="session-1",
        incognito=True,
    )
    return ctx


@pytest.mark.asyncio
async def test_build_chat_context_attaches_prep_timings(monkeypatch, caplog):
    ctx = await _build_context(
        monkeypatch, caplog,
        preface_timings={"memory": 0.25, "rag": 1.5},
    )

    timings = ctx.prep_timings
    assert timings["preprocess"] >= 0.0
    assert timings["preface"] >= 0.0
    assert timings["total"] >= timings["preface"]
    # Sub-stage timings recorded by the processor are merged in verbatim.
    assert timings["memory"] == 0.25
    assert timings["rag"] == 1.5
    # Incognito persists to the request-local transcript and still compacts.
    assert timings["history_persist"] >= 0.0
    assert timings["compaction"] >= 0.0


@pytest.mark.asyncio
async def test_build_chat_context_logs_prep_timing_line(monkeypatch, caplog):
    await _build_context(monkeypatch, caplog)

    lines = [r.getMessage() for r in caplog.records if "[chat-prep-timing]" in r.getMessage()]
    assert len(lines) == 1
    assert "preprocess" in lines[0]
    assert "total" in lines[0]


# --------------------------------------------------------------------------- #
# The stream path must surface the timings as a chat_prep SSE event
# --------------------------------------------------------------------------- #

def test_chat_stream_emits_chat_prep_event():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "routes" / "chat_routes.py").read_text()
    assert "'type': 'chat_prep'" in source
    assert "ctx.prep_timings" in source
