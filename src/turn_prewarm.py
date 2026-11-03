"""Prewarm message-independent inputs for the next chat turn.

Two trigger points (Task 2 follow-up, 2026-11-03):

- **Session creation / model switch** (`routes/session_routes.py`): the
  first turn's fixed costs — context-window resolution, the base agent
  prompt (byte-stable since the full-inventory change), the MCP schema
  surface — are paid while the user is still typing.
- **Response completed** (`routes/chat_helpers.run_post_response_tasks`):
  the compaction check runs on the post-response history. Under the
  threshold it is a cheap token estimate; over it, the LLM summarization
  runs NOW — while the user reads the answer — instead of blocking the
  next send. It joins the sequential extraction queue so it never competes
  with the next turn's request (#2927).

Everything is best-effort and single-flight per session: failures log at
DEBUG and never surface. Disable with ``ODYSSEUS_TURN_PREWARM=0``.
"""
import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget tasks: asyncio only keeps weak
# references, so without this the GC can collect a task mid-flight (same
# pattern as src/builtin_mcp.py / routes/chat_helpers.py).
_BG_TASKS: set = set()
_INFLIGHT: dict = {}
# The app's main event loop, captured at startup. Sync routes (FastAPI runs
# `def` routes in a threadpool with NO running loop) schedule prewarm
# coroutines onto this loop via run_coroutine_threadsafe.
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
# Sessions whose post-response compaction prewarm is currently running. The
# next turn's context build awaits it (``wait_for_compaction``) so the send
# path never snapshots half-compacted history or repeats the LLM call.
_COMPACT_INFLIGHT: dict = {}


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Capture the app's event loop (called once from app startup)."""
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def turn_prewarm_enabled(environ: Optional[dict] = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("ODYSSEUS_TURN_PREWARM", "").strip().lower()) not in {
        "0", "false", "no", "off",
    }


def _spawn_bg(coro):
    """Schedule a fire-and-forget coroutine, with or without a running loop.

    With a running loop (async context): a plain task. Without one (sync
    route threadpool): handed to the captured app loop via
    ``run_coroutine_threadsafe`` — a bare ``asyncio.create_task`` there
    raises RuntimeError, which callers used to swallow, silently turning
    the session-creation prewarm into a no-op.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
        return task

    target = _MAIN_LOOP
    if target is None or target.is_closed():
        coro.close()
        logger.debug("turn prewarm dropped: no event loop available")
        return None
    future = asyncio.run_coroutine_threadsafe(coro, target)
    _BG_TASKS.add(future)
    future.add_done_callback(_BG_TASKS.discard)
    return future


def schedule_session_prewarm(sess, owner: Optional[str] = None) -> None:
    """Fire-and-forget prewarm for the session's next turn; single-flight."""
    if not turn_prewarm_enabled():
        return
    session_id = str(getattr(sess, "id", "") or "")
    if not session_id or session_id in _INFLIGHT:
        return

    async def _run():
        try:
            await prewarm_session_turn(sess, owner)
        except Exception:
            logger.debug("turn prewarm failed (non-critical)", exc_info=True)
        finally:
            _INFLIGHT.pop(session_id, None)

    _INFLIGHT[session_id] = True
    _spawn_bg(_run())


async def prewarm_session_turn(sess, owner: Optional[str] = None) -> None:
    """Pay the message-independent fixed costs of the next turn now.

    Only message-INDEPENDENT work is prewarmed. The per-turn parts that
    genuinely need the user's text (tool-RAG selection, memory recall,
    preprocess) cannot be moved off the send path.
    """
    endpoint = str(getattr(sess, "endpoint_url", "") or "")
    model = str(getattr(sess, "model", "") or "")
    if not endpoint or not model:
        return

    # 1. Context window for (endpoint, model): may probe the endpoint's
    #    catalog once; afterwards the per-turn lookup is a cache hit.
    try:
        from src.model_context import get_context_length
        get_context_length(endpoint, model)
    except Exception:
        logger.debug("context-length prewarm skipped", exc_info=True)

    # 2. Base agent prompt for the common turn shape. Since the
    #    full-inventory change the system prompt is a pure function of
    #    (disabled, needs_admin, compact) — stable per session — so one
    #    build now fills `_cached_base_prompt` and the next send reuses the
    #    cached bytes. The disabled set mirrors the common agent turn:
    #    global toggles + owner blocks + email alias expansion. A turn with
    #    a different shape (plan mode, admin intent, ...) just builds fresh.
    try:
        from src import agent_loop as al
        from src.settings import get_setting
        from src.tool_security import blocked_tools_for_owner

        disabled = set(get_setting("disabled_tools", []) or [])
        disabled |= blocked_tools_for_owner(owner)
        disabled = al._expand_disabled_email_aliases(disabled)

        try:
            mcp_mgr = al.get_mcp_manager()
        except Exception:
            mcp_mgr = None

        al._build_system_prompt(
            [{"role": "user", "content": "prewarm"}],
            model=model,
            active_document=None,
            mcp_mgr=mcp_mgr,
            disabled_tools=disabled,
            needs_admin=False,
            relevant_tools=None,
            owner=owner,
            endpoint_url=endpoint,
        )
    except Exception:
        logger.debug("base-prompt prewarm skipped", exc_info=True)

    # 3. MCP schema surface — the same assembly the first turn performs
    #    (per-server disabled map from the DB + qualified schemas).
    try:
        from src import agent_loop as al

        mcp_mgr = al.get_mcp_manager()
        if mcp_mgr is not None:
            mcp_mgr.get_all_openai_schemas(al._load_mcp_disabled_map())
    except Exception:
        logger.debug("MCP schema prewarm skipped", exc_info=True)


async def prewarm_compaction(sess, owner: Optional[str] = None) -> None:
    """Run the compaction check on the post-response history.

    The next turn's ``maybe_compact`` then sees an already-compacted (or
    confirmed-under-threshold) history and no-ops, so the send path never
    waits for the compaction LLM call. Registered in ``_COMPACT_INFLIGHT``
    so a send that starts mid-compaction awaits it
    (``wait_for_compaction``) instead of racing it.
    """
    endpoint = str(getattr(sess, "endpoint_url", "") or "")
    model = str(getattr(sess, "model", "") or "")
    session_id = str(getattr(sess, "id", "") or "")
    if not endpoint or not model:
        return
    history = None
    getter = getattr(sess, "get_context_messages", None)
    if callable(getter):
        try:
            history = getter()
        except Exception:
            history = None
    if not history:
        history = list(getattr(sess, "messages", []) or [])
    if not history:
        return
    from src.context_compactor import maybe_compact

    fut: Optional[asyncio.Future] = None
    if session_id:
        try:
            fut = asyncio.get_running_loop().create_future()
            _COMPACT_INFLIGHT[session_id] = fut
        except RuntimeError:
            fut = None
    try:
        await maybe_compact(
            sess,
            endpoint,
            model,
            history,
            getattr(sess, "headers", None),
            owner=owner,
        )
    finally:
        if session_id:
            _COMPACT_INFLIGHT.pop(session_id, None)
        if fut is not None and not fut.done():
            fut.set_result(None)


async def wait_for_compaction(session_id: str) -> None:
    """Await an in-flight post-response compaction prewarm for a session.

    Called at the top of ``build_chat_context`` so the context build never
    snapshots history that the prewarm is about to rewrite, and never
    repeats a compaction the prewarm is already performing. Returns
    immediately when nothing is in flight.
    """
    fut = _COMPACT_INFLIGHT.get(str(session_id or ""))
    if fut is not None:
        await fut
