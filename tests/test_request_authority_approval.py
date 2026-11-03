"""An ungranted-but-not-explicitly-denied operation becomes an interactive
approval card instead of the hard "outside server request authority" error.
Explicit denials (policy, denied set, block_all) keep hard-denying, and
external-context proposals keep their armed-run claim requirement.
"""
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

import src.agent_loop as al
import src.agent_tools
from src.agent_runtime.authority import (
    MISSING_AUTHORITY, ExactOperation, OperationGrant, RequestAuthority,
    active_request_authority, widen_authority_for_approval,
    with_request_authority,
)
from src.tool_approval_scopes import TASK_APPROVAL_DECISION
from src.tool_approvals import (
    PROPOSAL_KIND_EXTERNAL_CONTEXT,
    PROPOSAL_KIND_REQUEST_AUTHORITY,
    tool_approval_store,
)
from src.tool_capabilities import capabilities_for_action
from src.tool_execution import (
    NO_TOOL_SECURITY_CONTEXT, classify_request_admission, execute_tool_block,
)
from src.tool_policy import ToolPolicy
from src.tool_types import ToolBlock


def authority(*tools, owner="alice", session_id="s", workspace=""):
    return RequestAuthority("request-test", owner, session_id, workspace,
                            tuple(OperationGrant(tool) for tool in tools))


async def _collect(gen):
    return [c async for c in gen]


# ── classification helper ────────────────────────────────────────────────────

def test_ungranted_operation_is_distinct_from_explicit_denials():
    block = ToolBlock("web_fetch", "https://example.test/page")

    ungranted = classify_request_admission(
        block, owner="alice", session_id="s",
        request_authority=authority("web_search"))
    assert ungranted.status == "ungranted"
    assert "outside server request authority" in ungranted.reason

    denied_set = classify_request_admission(
        block, owner="alice", session_id="s",
        request_authority=replace(
            authority("web_search"), denied=frozenset({"web_fetch"})))
    assert denied_set.status == "denied"

    from src.tool_policy import ToolPolicy
    block_all = classify_request_admission(
        block, owner="alice", session_id="s",
        tool_policy=ToolPolicy(block_all_tool_calls=True),
        request_authority=authority("web_search"))
    assert block_all.status == "denied"

    granted = classify_request_admission(
        block, owner="alice", session_id="s",
        request_authority=authority("web_fetch"))
    assert granted.status == "admitted"

    unbound = classify_request_admission(
        block, owner="alice", session_id="s", request_authority=None)
    assert unbound.status == "ungranted"


def test_exact_approval_admits_the_sealed_ungranted_operation():
    content = "https://example.test/page"
    # Authority, approval, and classification must share one session: the
    # authority is bound per session and the digest seals it.
    auth = authority("web_search", session_id="exact-match")
    pending = tool_approval_store.create(
        owner="alice", session_id="exact-match", origin_run_id="run",
        tool_name="web_fetch", content=content, workspace="",
        external_untrusted_context_seen=False,
        proposal_kind=PROPOSAL_KIND_REQUEST_AUTHORITY,
        capabilities=capabilities_for_action("web_fetch", content),
        request_authority=auth,
    )
    exact = tool_approval_store.consume(
        pending.approval_id, decision="approve",
        owner="alice", session_id="exact-match")
    assert exact is not None

    decision = classify_request_admission(
        ToolBlock("web_fetch", content), owner="alice",
        session_id="exact-match",
        request_authority=auth, exact_approval=exact)
    assert decision.status == "admitted"
    assert decision.exact_admission is True


# ── proposal kind on the pending approval ────────────────────────────────────

def _create_pending(session, *, kind, seen=False, request_authority=None):
    content = "https://example.test/page"
    return tool_approval_store.create(
        owner="alice", session_id=session, origin_run_id="run",
        tool_name="web_fetch", content=content, workspace="",
        external_untrusted_context_seen=seen,
        proposal_kind=kind,
        capabilities=capabilities_for_action("web_fetch", content),
        request_authority=request_authority,
    )


def test_request_authority_proposal_seals_kind_and_card_copy():
    pending = _create_pending(
        "kind-auth", kind=PROPOSAL_KIND_REQUEST_AUTHORITY)
    assert pending.proposal_kind == PROPOSAL_KIND_REQUEST_AUTHORITY
    payload = pending.public_payload()
    assert payload["proposal_kind"] == PROPOSAL_KIND_REQUEST_AUTHORITY
    assert "didn't explicitly authorize" in payload["description"]

    external = _create_pending(
        "kind-external", kind=PROPOSAL_KIND_EXTERNAL_CONTEXT, seen=True)
    assert external.proposal_kind == PROPOSAL_KIND_EXTERNAL_CONTEXT
    external_payload = external.public_payload()
    assert external_payload["proposal_kind"] == PROPOSAL_KIND_EXTERNAL_CONTEXT
    assert "Untrusted context" in external_payload["description"]

    with pytest.raises(ValueError):
        _create_pending("kind-bogus", kind="bogus")
    options = {o["value"]: o["description"] for o in payload["options"]}
    assert "same tool" in options[TASK_APPROVAL_DECISION]
    external_options = {
        o["value"]: o["description"] for o in external_payload["options"]
    }
    assert "every otherwise-gated" in external_options[TASK_APPROVAL_DECISION]


# ── claim requirements ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_authority_proposal_claims_without_armed_context(monkeypatch):
    content = "https://example.test/page"
    auth = authority("web_search", session_id="claim-auth")
    pending = tool_approval_store.create(
        owner="alice", session_id="claim-auth", origin_run_id="run",
        tool_name="web_fetch", content=content, workspace="",
        external_untrusted_context_seen=False,
        proposal_kind=PROPOSAL_KIND_REQUEST_AUTHORITY,
        capabilities=capabilities_for_action("web_fetch", content),
        request_authority=auth,
    )
    assert pending.backend_operation is not None
    exact = tool_approval_store.consume(
        pending.approval_id, decision="approve",
        owner="alice", session_id="claim-auth")
    assert exact is not None

    handler = AsyncMock(
        return_value={"output": "page text", "exit_code": 0})
    monkeypatch.setitem(src.agent_tools.TOOL_HANDLERS, "web_fetch", handler)
    _, result = await execute_tool_block(
        ToolBlock("web_fetch", content), owner="alice", session_id="claim-auth",
        security_context=NO_TOOL_SECURITY_CONTEXT, request_authority=auth,
        exact_approval=exact)
    assert result.get("exit_code") == 0, result
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_external_context_proposal_still_requires_armed_context():
    content = "https://example.test/page"
    auth = authority("web_fetch", session_id="claim-external")
    pending = tool_approval_store.create(
        owner="alice", session_id="claim-external", origin_run_id="run",
        tool_name="web_fetch", content=content, workspace="",
        external_untrusted_context_seen=True,
        proposal_kind=PROPOSAL_KIND_EXTERNAL_CONTEXT,
        capabilities=capabilities_for_action("web_fetch", content),
        request_authority=auth,
    )
    exact = tool_approval_store.consume(
        pending.approval_id, decision="approve",
        owner="alice", session_id="claim-external")
    assert exact is not None

    _, result = await execute_tool_block(
        ToolBlock("web_fetch", content), owner="alice",
        session_id="claim-external",
        security_context=NO_TOOL_SECURITY_CONTEXT, request_authority=auth,
        exact_approval=exact)
    assert result.get("policy") == "exact_tool_approval"
    assert "armed run security context" in str(result.get("error", ""))


# ── agent loop: card instead of hard error ───────────────────────────────────

def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                out.append(json.loads(chunk[6:]))
            except Exception:
                pass
    return out


def _patch_loop_basics(monkeypatch):
    monkeypatch.setattr(
        al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)


@pytest.mark.asyncio
async def test_ungranted_web_fetch_gets_approval_card_not_error(monkeypatch):
    _patch_loop_basics(monkeypatch)
    executed = []

    async def _fake_stream(_candidates, messages, **kwargs):
        yield (
            "data: "
            + json.dumps({
                "type": "tool_calls",
                "calls": [{
                    "id": "call_fetch",
                    "name": "web_fetch",
                    "arguments": json.dumps(
                        {"url": "https://www.spacex.com/launches/"}),
                }],
            })
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    async def _fail_exec(block, *args, **kwargs):
        executed.append(block)
        return "web_fetch", {"output": "should not run", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fail_exec, raising=False)

    chunks = await _collect(al.stream_agent_loop(
        "https://api.openai.com/v1",
        "local-model",
        [{"role": "user", "content": "open the official SpaceX Starship launches page"}],
        max_rounds=2,
        relevant_tools={"web_search", "web_fetch"},
    ))
    events = _events(chunks)
    assert executed == []

    cards = [e for e in events if e.get("type") == "ask_user"]
    assert cards, events
    card = cards[0]["data"]
    assert card["kind"] == "tool_approval"
    assert card["proposal_kind"] == "request_authority"
    assert "didn't explicitly authorize" in card["description"]
    assert card["action"]["tool"] == "web_fetch"


@pytest.mark.asyncio
async def test_granted_web_fetch_still_executes_without_a_card(monkeypatch):
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield (
            "data: "
            + json.dumps({
                "type": "tool_calls",
                "calls": [{
                    "id": "call_fetch",
                    "name": "web_fetch",
                    "arguments": json.dumps(
                        {"url": "https://www.spacex.com/launches/"}),
                }],
            })
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    async def _fake_exec(block, *args, **kwargs):
        return "web_fetch", {"output": "page text", "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    chunks = await _collect(al.stream_agent_loop(
        "https://api.openai.com/v1",
        "local-model",
        [{
            "role": "user",
            "content": (
                "web_fetch https://www.spacex.com/launches/ and summarize "
                "the launches listed there"
            ),
        }],
        max_rounds=2,
        relevant_tools={"web_search", "web_fetch"},
    ))
    events = _events(chunks)
    assert not [e for e in events if e.get("type") == "ask_user"]
    outputs = [e for e in events if e.get("type") == "tool_output"]
    assert outputs and outputs[0]["tool"] == "web_fetch"


# ── task-scope continuation widens the resumed authority ────────────────────

@with_request_authority
async def _bound_authority(
    messages=None,
    owner=None,
    session_id=None,
    workspace=None,
    tool_policy=None,
    disabled_tools=None,
    exact_approval=None,
    request_authority=MISSING_AUTHORITY,
    **_rest,
):
    # Named parameters are required: with_request_authority introspects the
    # bound signature arguments, which a kwargs-only function collapses into
    # one entry.
    yield active_request_authority()


def _consume_pending(
    session,
    *,
    decision="approve_task",
    allow_continuation=True,
    request_authority=None,
    tool="web_fetch",
    content="https://example.test/page",
):
    pending = tool_approval_store.create(
        owner="alice", session_id=session, origin_run_id="run",
        tool_name=tool, content=content, workspace="",
        external_untrusted_context_seen=False,
        proposal_kind=PROPOSAL_KIND_REQUEST_AUTHORITY,
        capabilities=capabilities_for_action(tool, content),
        request_authority=request_authority,
    )
    return tool_approval_store.consume(
        pending.approval_id, decision=decision,
        owner="alice", session_id=session,
        allow_continuation=allow_continuation)


@pytest.mark.asyncio
async def test_task_scope_approval_widens_the_resumed_authority():
    auth = authority("web_search", session_id="widen-task")
    exact = _consume_pending("widen-task", request_authority=auth)
    assert exact.allow_remaining_actions is True

    widened = (await _collect(_bound_authority(
        exact_approval=exact,
        owner="alice", session_id="widen-task", workspace="")))[0]

    assert widened.permits(
        ExactOperation.normalize("web_fetch", "https://example.test/other"))


@pytest.mark.asyncio
async def test_action_classified_approval_widens_to_the_approved_action_only():
    auth = authority("get_workspace", session_id="widen-action")
    exact = _consume_pending(
        "widen-action", request_authority=auth,
        tool="manage_memory", content='{"action":"list"}')

    widened = (await _collect(_bound_authority(
        exact_approval=exact,
        owner="alice", session_id="widen-action", workspace="")))[0]

    assert widened.permits(
        ExactOperation.normalize("manage_memory", '{"action":"list"}'))
    assert not widened.permits(
        ExactOperation.normalize("manage_memory", '{"action":"add"}'))


@pytest.mark.asyncio
async def test_single_action_scope_does_not_widen():
    auth = authority("web_search", session_id="widen-single")
    exact = _consume_pending(
        "widen-single", request_authority=auth, allow_continuation=False)
    assert exact.allow_remaining_actions is False

    widened = (await _collect(_bound_authority(
        exact_approval=exact,
        owner="alice", session_id="widen-single", workspace="")))[0]

    assert not widened.permits(
        ExactOperation.normalize("web_fetch", "https://example.test/other"))


@pytest.mark.asyncio
async def test_widening_never_lifts_an_explicit_denial():
    auth = authority("web_search", session_id="widen-denied")
    exact = _consume_pending("widen-denied", request_authority=auth)

    widened = (await _collect(_bound_authority(
        exact_approval=exact,
        owner="alice", session_id="widen-denied", workspace="",
        tool_policy=ToolPolicy(disabled_tools=frozenset({"web_fetch"})))))[0]

    assert not widened.permits(
        ExactOperation.normalize("web_fetch", "https://example.test/other"))


def test_widening_skips_a_tool_the_authority_already_grants():
    auth = authority("web_fetch", session_id="widen-dup")
    exact = _consume_pending("widen-dup", request_authority=auth)
    assert widen_authority_for_approval(auth, exact) is auth


# ── per-run proposal bounds ──────────────────────────────────────────────────

def _fetch_call(index, url="https://www.spacex.com/launches/"):
    return {
        "id": f"call_fetch_{index}",
        "name": "web_fetch",
        "arguments": json.dumps({"url": url}),
    }


def _fetch_outputs(events):
    return [
        e for e in events
        if e.get("type") == "tool_output" and e.get("tool") == "web_fetch"
    ]


@pytest.mark.asyncio
async def test_first_card_pauses_the_run_for_a_decision(monkeypatch):
    """One decision per run: the run stops at the first card instead of
    dispatching further ungranted operations or stacking more cards."""
    _patch_loop_basics(monkeypatch)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield (
            "data: "
            + json.dumps({
                "type": "tool_calls",
                "calls": [
                    _fetch_call(1, "https://example.test/one"),
                    _fetch_call(2, "https://example.test/two"),
                    _fetch_call(3, "https://example.test/three"),
                ],
            })
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    async def _fail_exec(block, *args, **kwargs):
        raise AssertionError("ungranted operation reached the executor")

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fail_exec, raising=False)

    chunks = await _collect(al.stream_agent_loop(
        "https://api.openai.com/v1",
        "local-model",
        [{"role": "user", "content": "open the official SpaceX Starship launches page"}],
        max_rounds=2,
        relevant_tools={"web_search", "web_fetch"},
    ))
    events = _events(chunks)
    outputs = _fetch_outputs(events)
    assert len(outputs) == 1
    assert outputs[0].get("ask_user", {}).get("kind") == "tool_approval"
    assert not [e for e in events if e.get("type") == "tool_start"]


@pytest.mark.asyncio
async def test_run_denied_operation_gets_the_error_without_a_card(monkeypatch):
    _patch_loop_basics(monkeypatch)
    content = json.dumps({"url": "https://www.spacex.com/launches/"})
    denied = al._tool_call_signature("web_fetch", content)

    async def _fake_stream(_candidates, messages, **kwargs):
        yield (
            "data: "
            + json.dumps({"type": "tool_calls", "calls": [_fetch_call(1)]})
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    async def _fail_exec(block, *args, **kwargs):
        raise AssertionError("denied operation reached the executor")

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _fail_exec, raising=False)

    chunks = await _collect(al.stream_agent_loop(
        "https://api.openai.com/v1",
        "local-model",
        [{"role": "user", "content": "open the official SpaceX Starship launches page"}],
        max_rounds=2,
        relevant_tools={"web_search", "web_fetch"},
        run_denied_signatures={denied},
    ))
    events = _events(chunks)
    outputs = _fetch_outputs(events)
    # The model retries the denied operation in round 2; both attempts get
    # the error result and neither shows a card.
    assert len(outputs) == 2
    assert all(e.get("blocked") is True for e in outputs)
    assert not any(e.get("ask_user") for e in outputs)
    assert all("denied by the user" in e["output"] for e in outputs)
