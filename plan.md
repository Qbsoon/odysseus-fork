# Odysseus Project Plan

## Current task: "The exact operation is outside server request authority." must never reach the user without an approval prompt

### Verified root cause

1. **Error source** — `execute_tool_block` in `src/tool_execution.py` (~lines 1690–1730):
   when `authority.permits(operation)` is false and no exact approval matches, it returns
   `failure_kind: "request_authority_denied"` with reason
   *"The exact operation is outside server request authority."* — a hard error handed
   straight back to the model. There is no interactive path for this case.

2. **Grants derive only from the user's request text** — `create_request_authority`
   (`src/agent_runtime/authority.py` ~line 371) → `interpret_request` →
   `requested_capabilities` (`src/turn_contract.py`). A model-initiated action outside
   the semantic intent of the user's message (e.g. `web_fetch` when the prompt didn't
   imply web intent; `manage_memory` add when only a list/read was implied) gets **no
   grant** and therefore hard-fails.

3. **The only approval card in the agent loop arms on a different gate** —
   `src/agent_loop.py` (~lines 33109–33450) proposes an approval only when
   `run_security.decision_for(...)` blocks, i.e. the *external-untrusted-context* gate
   (`ToolRunSecurityContext`, `src/tool_capabilities.py`). Authority non-admission has
   no prompt branch at all.

4. **Approval continuations reuse the proposal-time authority** —
   `routes/chat_routes.py` (~line 3573) sets
   `_request_authority = exact_tool_approval.pending.request_authority`. The TASK scope
   ("Allow for this task") only bypasses the external-context gate
   (`approval_gate_bypassed`), **not** the authority grants. This explains the memory
   case: list was approved and executed via exact admission, then add (a different
   action/tool) was outside the sealed grants → hard error, no second card.

5. **The exact-approval claim path requires an "armed" security context** —
   `src/tool_execution.py` (~line 1826) demands `external_untrusted_context_seen` on both
   the run and the pending approval. A new authority-based approval therefore cannot
   reuse the existing claim path unchanged.

### Observed scenarios explained

- **Memory list → add**: the card came from the external-context gate; the sealed
  authority (derived from the original user text) never included the add action, so the
  follow-up in the same task hard-failed.
- **Web fetch ×2 in one run**: the prompt implied no web intent → no `web_fetch`
  grant; the security gate never armed (read-only tool) → both attempts returned the
  hard error with no card.

### Design constraints

- **Explicit denials stay hard denials** (never prompt-able): tool-policy blocks,
  `authority.restricted()` (denied set / `block_all` / `disable_mcp`), delegated API
  credentials, unattended runs (no human to ask; `ask_user` already disabled there).
- Only the **"ungranted but not explicitly denied"** case converts to an approval prompt.
- Approvals must keep sealing the exact action + resource identities at proposal time
  (existing machinery in `tool_approval_store.create`).

---

## Phase 1 — Machine-readable denial classification

- [x] In `execute_tool_block` (`src/tool_execution.py`), split the generic
      "outside server request authority" denial into a distinct marker (e.g.
      `failure_kind: "request_authority_ungranted"`) vs. the policy/restricted denials
      (which keep `request_authority_denied`). No string matching later.
      DONE 2026-11-02: the `if not admitted:` branch now classifies — email-policy
      block and `authority.restricted(operation)` keep `request_authority_denied`;
      everything else (no grant / unbound authority) is
      `request_authority_ungranted`. The invalid-arguments branch (bad JSON input,
      a different error path) intentionally keeps `request_authority_denied`.
      `blocked: True` is still set on every denial, so the `endswith("_denied")`
      checks in `src/agent_runtime/journal.py` and
      `src/agent_runtime/effect_adapters.py` are unaffected.
- [x] Update existing assertions on `failure_kind` in:
      `tests/test_request_authority.py` (1 assertion + new
      `test_ungranted_and_restricted_denials_are_distinguishable` covering
      ungranted / denied-set / block_all),
      `tests/test_runtime_resource_integration.py` (3),
      `tests/test_background_containment.py` (1),
      `tests/test_wave3_diagnostics.py` (no change needed — its assertion covers
      the invalid-arguments branch, which keeps the hard denial marker),
      `tests/test_resource_identity.py` (3).

## Phase 2 — Approval card for ungranted operations (core fix)

- [x] Extract the admission decision (normalize → permits / restrict / exact-approval
      match) from `execute_tool_block` into a reusable helper so the agent loop can
      pre-check without duplicating logic; `execute_tool_block` keeps it as
      defense-in-depth.
      DONE 2026-11-03: `classify_request_admission` + `AdmissionDecision` in
      `src/tool_execution.py`; `execute_tool_block` now calls it (single source
      of truth, no duplicated classification).
- [x] In the `src/agent_loop.py` dispatch chain (if/elif around lines 33100–33500), add
      a branch: operation ungranted-but-not-restricted (and not delegated/unattended) →
      propose an approval via `tool_approval_store.create` with the same sealing as the
      security-gate branch (selected tools, continuation query, request authority,
      client runtime context), return `approval_required` + `ask_user` card with an
      authority-specific reason text, and pause the run.
      DONE 2026-11-03: pre-check computed before the dispatch chain; new `elif`
      branch after the external-context gate branch fires only on
      `status == "ungranted"` and pauses via the existing `_awaiting_user`
      machinery. The pre-check is skipped when no request authority is bound
      (those surfaces keep the hard error), when the bound authority is
      `inherited` (a child cannot seal an ungranted action — the proposal
      would never execute after approval), for delegated credentials,
      unattended native runs, teacher runs, the synthetic local-media
      observation, and explicit policy/disabled denials. The branch also
      carries the gate branch's unsealed-document guard: an ungranted
      edit/suggest/update_document with no open document blocks instead of
      proposing a card that could never execute.
- [x] Add a proposal kind to `PendingToolApproval` (`src/tool_approvals.py`), e.g.
      `proposal_kind: "external_context" | "request_authority"`, included in the digest
      payload; card copy differs per kind ("your request didn't explicitly authorize
      this action" vs. the current untrusted-context wording).
      DONE 2026-11-03: `proposal_kind` is digest-bound (in `_binding_payload`
      and `_matches_unlocked`), validated in `create`, and exposed in
      `public_payload` with kind-specific default copy.
- [x] Relax the "armed run security context" claim requirement in `execute_tool_block`
      (~line 1826) for `request_authority` proposals; external-context proposals keep it.
      DONE 2026-11-03: only `external_context` proposals require the armed
      context on run and pending; `request_authority` proposals claim against
      the re-bound sealed authority.
- [x] Verify the resume path end-to-end: `routes/chat_routes.py` restores the sealed
      authority + workspace; `with_request_authority` clears `inherited` when parent is
      None and an exact approval is present; `exact_admission` then admits the sealed
      action.
      DONE 2026-11-03 (static verification): `with_request_authority`
      (`src/agent_runtime/authority.py`) uses `approval.pending.request_authority`
      and clears `inherited` when parent is None and an exact approval is
      present; `routes/chat_routes.py` already restores the sealed authority
      and workspace for any consumed approval, so `request_authority`
      proposals resume through the same path. A full suite run is still
      required to catch loop tests whose fake executors relied on executing
      ungranted tools — those now correctly get a card; the fix pattern is to
      name the tool in the test's request text (done for
      `test_web_fetch_js_failure_exposes_private_browser_next_round`) or bind
      a granting authority.
- [x] New tests: `tests/test_request_authority_approval.py` — classification
      (ungranted vs denied-set vs block_all vs granted vs unbound), exact
      approval admits the sealed ungranted operation, proposal-kind sealing +
      card copy + invalid kind rejection, `request_authority` proposal claims
      without an armed context while `external_context` still requires it,
      loop-level ungranted `web_fetch` → card (not execution), and granted
      `web_fetch` still executes without a card. Review follow-up 2026-11-03:
      aligned authority/approval/execution session ids in the three
      store/claim tests (they were deterministically failing on session
      mismatch) and removed unused imports. NOT yet executed — run
      `tests/test_request_authority_approval.py tests/test_tool_policy.py
      tests/test_external_context_tool_gate.py tests/test_request_authority.py
      tests/test_tool_approvals.py`, then the full suite to audit loop tests
      whose fake executors relied on executing ungranted tools (they now get
      a card; fix pattern: name the tool in the request text, as done for
      `test_web_fetch_js_failure_exposes_private_browser_next_round`).

## Phase 3 — Task-scope continuation semantics (decision required)

DECIDED 2026-11-03: Option B, as recommended.

- [x] Implement Option B: a TASK-scope approval adds an `OperationGrant` for the
      approved tool to the resumed run's authority — action-level where the approved
      action is action-classified (e.g. `manage_memory` list), tool-level otherwise
      (e.g. `web_fetch`) — matching the granularity `create_request_authority` would
      have used. Explicit denials remain un-liftable.
      DONE 2026-11-03: `widen_authority_for_approval` in
      `src/agent_runtime/authority.py`, applied in `with_request_authority`
      right after the inherited-clear and BEFORE the policy restriction, so
      `restrict()` keeps every explicit denial authoritative. The grant is
      deduplicated (a tool the authority already grants is left alone —
      `RequestAuthority` forbids duplicate tool grants). One-action scope
      (`allow_remaining_actions=False`, e.g. the skills tester) never widens.
- [x] Define the CHAT_SESSION analog: the resumed run widens exactly like
      TASK scope. Later runs in the same session keep the existing behavior —
      the persisted session marker bypasses only the external-context gate —
      because the marker records the decision, not the approved tool, and
      reconstructing grants from history is out of scope. The card's
      chat-session option copy for `request_authority` proposals states this
      explicitly ("Later requests in this chat also skip the untrusted-context
      gate"), and the task-option copy now promises the same tool for this
      request rather than "every otherwise-gated action".

## Phase 4 — Loop protection

- [x] After a Deny (or N repeated proposals of the same operation), mark the operation
      run-denied so later attempts get the hard error without another card.
      (With Task 3, deny now *continues* the run — this marker is what stops the
      model from re-prompting for the same action inside that continued run; see
      Task 3 Phase B.)
      DONE 2026-11-03: per-run state lives in the loop invocation itself
      (`_authority_proposed_signatures`, `_authority_proposal_count`), so a new
      user message (a new run) starts clean and the same action may be proposed
      and carded again — nothing is persisted. A denied operation is seeded
      into the resumed run via the new `run_denied_signatures` loop parameter
      (Task 3 Phase B wires the deny path to pass it); a seeded signature gets
      the error result with `policy: "user_denied"` and no card. Retries of an
      already-proposed operation get the error result with
      `failure_kind: "request_authority_ungranted"` and no card.
- [x] Cap approval proposals per run; confirm delegated/unattended contexts keep the
      hard denial.
      DONE 2026-11-03, revised per user decision: there is NO proposal counter
      and no per-run cap — the run pauses at the first card, so one decision
      is taken per run, and a run that needs repeated fetches gets them
      (different URLs each get their own card; the same URL after approval
      executes freely via the Phase 3 widening). The ONLY per-run silencing
      is an explicit user Deny: a denied operation is seeded into the
      continued run via `run_denied_signatures` (Task 3 Phase B wires the
      deny path) and retries get the error result with `policy:
      "user_denied"`, never a second card. Delegated/unattended/teacher/
      no-bound-authority/inherited surfaces never reach the proposal branch
      (pre-check skip conditions, Phase 2), so they keep the hard denial.
- [x] Tests: the first card pauses the run (one card, no further dispatch, no
      execution); a seeded denied signature → error result with no card,
      including the round-2 retry.

## Phase 5 — Tests & docs

- [x] New tests: ungranted `web_fetch` → card, not error; approving an
      `request_authority` proposal executes the sealed action with no external taint;
      task-scope second action (per Phase 3 choice); deny-then-retry hard-denies;
      policy/restricted/delegated still hard-deny.
      DONE 2026-11-03: all covered in `tests/test_request_authority_approval.py`
      (ungranted → card; claim without armed context; task-scope widening
      incl. action-scoping and denial non-lifting; first-card pause; seeded
      deny → error with no card incl. the round-2 retry) and
      `tests/test_request_authority.py` (denied-set / block_all keep the hard
      marker). Delegated/unattended hard-denial is enforced by the pre-check
      skip conditions (Phase 2).
- [x] Update `THREAT_MODEL.md` and `docs/AGENT_TURN_CONTRACT.md` for the two-gate model
      (external-context gate + request authority) and the new interactive authority
      admission.
      DONE 2026-11-03: `THREAT_MODEL.md` gains a "Request-authority admission"
      section (classification, prompt-ability, sealing, widening, deny
      semantics, chat-session scope limits) and the control-plane exemption
      now documents the brokered `web_fetch` carve-out.
      `docs/AGENT_TURN_CONTRACT.md` gains boundary item 7 for the admission
      semantics.

---

### Risks

- Prompt fatigue: distinct ungranted actions in a loop could spam cards → Phase 4 caps.
- Security review: task-scope widening must never lift explicit denials; resource
  identity sealing must remain at proposal time.
- `execute_tool_block` is also reached from background tasks, teacher escalation, and
  the skills tester (`allow_continuation=False`) — verify each surface still
  hard-denies or prompts appropriately.

### Next recommended task

Phase 1 (denial classification) — small, self-contained, unblocks Phases 2–5.

---

## Task 2 (2026-10-28): ~60s time-to-first-token on OpenCode Go in the app

**Symptom:** the app usually takes a whole minute to first token with OpenCode Go
(`https://opencode.ai/zen/go/v1`), while the same model in VS Code responds
immediately. Same endpoint, same model → the minute is attributable to the app's
request path, not the provider.

**Environment constraint:** this workspace is development-only — the app and
OpenCode Go cannot be run live from here. Everything I can do is: (a) static
code verification, (b) implement instrumentation + unit tests, (c) prepare a
user-executed reproduction runbook and analyze the logs the user pastes back.
Live attribution runs are executed by the user on their deployed instance.

### Verified facts from the code

1. **The first-token watchdog is local-only.** `_first_token_timeout`
   (`src/llm_core.py:215`) returns 0 for public hosts — OpenCode Go gets no
   watchdog, and more importantly **no TTFT logging**, so the delay is currently
   invisible in logs.
2. **Existing instrumentation covers only part of the path:**
   - `agent_prep` SSE event (`src/agent_loop.py:24643`) times `request_setup`,
     `tool_selection`, `prompt_build`, `context_trim` — but NOT the
     `routes/chat_routes.py` pre-flight (`build_chat_context`: memory recall,
     RAG embed/query, skills matching, web preprocessing) and NOT upstream TTFT.
   - `ODYSSEUS_DEBUG_LLM_SHAPE=1` already logs `[llm-first-token-frame]` shapes
     and `_debug_llm_request_shape` request shapes (`src/llm_core.py`).
3. **Cache identity is already stable:** `x-opencode-session` = the Odysseus
   session id (`_provider_headers`, `src/llm_core.py:1321`); the base agent
   prompt is cached in-process (`_cached_base_prompt`); the datetime block was
   deliberately moved out of the system prompt to keep the prefix byte-stable
   (issue #2927, `src/agent_loop.py:14562`).
4. **Thinking is unmapped for OpenCode Go:** `_apply_hosted_thinking_mode`
   (`src/llm_core.py`) only handles OpenRouter and Moonshot/Kimi. For
   `opencode-go` nothing is sent, so GLM-5.3's default thinking behavior applies.
   Thinking deltas do stream (`thinking=True` events via the harmony router),
   but whether they render immediately in practice is a live question.
5. **Other ~30–60s-shaped delays in the path** (candidates the timeline will
   surface if relevant): non-streaming pre-flight LLM calls (e.g. title
   generation, `timeout=15`, retries up to `MAX_RETRIES=3`), endpoint
   fallback/reroute chains that try another route before OpenCode Go
   (`resolve_foreground_model_policy`, `fallback_eligible`), the 20s dead-host
   cooldown, and connect timeouts (`LLM_CONNECT_TIMEOUT=10`).

### Ranked hypotheses

**User decision (2026-10-28): prompt size is NOT the issue — do not check or
limit it.** No prompt-size measurement, no trimming/clamping proposals.

- **H2 — Thinking transport/render:** GLM-5.3 may stream `reasoning_content`
  first. **Statically downgraded:** the frontend does render thinking deltas
  (`static/js/chat.js`, `liveThinkingThrottle.js`, the `<think>` parser in
  `markdown.js`). Remaining live questions: whether Go/GLM-5.3 emits
  `reasoning_content` at all over this transport, and whether thinking render
  is deferred/buffered in practice.
- **H3 — chat_routes pre-flight** (memory/RAG/skills/web preprocessing, and any
  pre-flight LLM calls) blocking before the stream starts — currently untimed;
  runtime cost not statically measurable.
- **H5 — Retry/reroute chain before the OpenCode Go request:** a fallback or
  foreground-policy reroute trying another route first, non-streaming pre-flight
  calls with retries, or cooldown/connect timeouts stacking up (see fact 5).
- **H4 — Per-turn catalog probes** (`resolve_effective_context`,
  `budget_context_for_model`). **Statically downgraded:** the agent route caches
  resolved context windows per (url, model) (`_route_context_lengths`,
  `src/agent_loop.py`), and `resolve_effective_context` runs once per compact
  turn — probes do not repeat per round. Keep only as a log-line check.

## Phase 2A — Diagnosis (no behavior change; live run is user-executed)

- [x] Add TTFT logging for ALL providers in the generic stream path
      (`src/llm_core.py`): time from request send → first SSE event, split by
      first-thinking vs first-content event, and log any retry/reroute/cooldown
      events on the way to the request. Mirror the existing
      `[llm-first-token-frame]` debug shape so it can run always at INFO, not
      only behind the env flag. (Implementable + unit-tested in this dev
      environment.)
      DONE 2026-11-03: `[llm-ttft]` INFO line in `_stream_llm_inner`'s
      OpenAI-compatible path (the transport OpenCode Go uses): `ttfb_ms`
      (request sent → response headers), `ttft_ms` (request sent → first
      *meaningful* frame — role-only/whitespace preambles don't stop the
      clock), `first=` classification via the new `_classify_first_stream_frame`
      (error > thinking > tool_calls > content), plus the first frame's
      delta_keys/content_chars/tool_calls/finish shape. Failure before any
      first event logs `failed_before_first_event=connect|read_timeout|<exc>`
      with elapsed ms. Retry/reroute/cooldown events were already logged
      (`[fallback]` warnings, cooldown 503s) — unchanged. Tests:
      `tests/test_llm_ttft_logging.py` (INFO without the debug flag,
      thinking/content/tool_calls classification, preamble not counted,
      classifier unit cases). NOT yet executed.
- [x] Extend timing coverage to the chat_routes pre-flight: emit one
      `chat_prep` SSE event (or fold into `agent_prep`) with
      `build_chat_context` stage timings (memory, RAG, skills, web preprocessing).
      (Implementable + unit-tested here.)
      DONE 2026-11-03: `ChatProcessor.build_context_preface` records per-stage
      timings into `_last_preface_timings` (memory / rag / web_search /
      url_fetch / skills — executed stages only); `build_chat_context`
      (`routes/chat_helpers.py`) adds `preprocess`, `history_persist`,
      `preface`, `compaction`, `total`, merges the sub-stages, attaches them
      as `ChatContext.prep_timings`, and logs `[chat-prep-timing]` at INFO.
      `routes/chat_routes.py` `stream_with_save` emits a `chat_prep` SSE event
      right after `turn_mode` (unknown event types are ignored by the
      frontend dispatcher, so this is additive). Tests:
      `tests/test_chat_prep_timing.py` (stage recording, executed-only keys,
      per-call reset, ctx attachment + merge, log line, static check of the
      SSE emission). NOT yet executed.
- [x] Write a short reproduction runbook for the user's live instance:
      set `ODYSSEUS_DEBUG_LLM_SHAPE=1`, send one agent-mode OpenCode Go turn,
      capture the new TTFT/`chat_prep`/`agent_prep` log lines, and run the
      comparison curl (same model, minimal prompt) against
      `https://opencode.ai/zen/go/v1`. One paste of the logs attributes the
      minute to H2/H3/H5 (H4 is a log-line check).
      DONE 2026-11-03: `docs/TTFT_REPRO_RUNBOOK.md` — signal table, capture
      steps, comparison curl (with the `x-opencode-session` header), and an
      observation → hypothesis → Phase 2B fix mapping. Includes the
      reference-extension findings (below).

### Reference: the fast VS Code path (opencode-copilot-chat)

Checked 2026-11-03 against `ltmoerdani/opencode-copilot-chat`'s provider
architecture doc (the extension the user's fast VS Code comparison runs
through):

- GLM (and most models) go over the **same** `/zen/go/v1` chat-completions
  transport the app uses — transport is not the difference.
- It sends an explicit per-family **thinking control**: GLM maps to
  `thinking: {type}` in the payload (Kimi/MiniMax similar; DeepSeek/Mimo map to
  reasoning effort). The app sends nothing for `opencode-go`
  (`_apply_hosted_thinking_mode` handles only OpenRouter and Kimi), so GLM-5.3
  runs with provider-default thinking — this is the concrete H2 fix shape for
  Phase 2B.
- It captures `reasoning_content` during streaming and echoes it back on
  assistant history for GLM (tolerated family; DeepSeek requires it).
- It bounds request budgets with proportional tokenizer headroom and tracks
  per-request latency summaries — consistent with the fast path being
  well-behaved on the same endpoint.
- [x] User runs the runbook on the live deployment; analyze the pasted logs
      here and select the Phase 2B fix.
      DONE 2026-11-03 (user pasted `test.log`, one agent-mode GLM-5.3-flash
      turn, ~54s to first token). Attribution:
      - **H3 CONFIRMED, dominant app-side cost: pre-flight web search = 31.9s**
        (`[chat-prep-timing] web_search=31.89 total=32.07`; client perf
        `chat_stream_headers=32831ms`). Breakdown from the log: query-gen
        non-streaming LLM call ~6.7s + SearXNG ~12.2s + 3 serial page fetches
        ~12.7s — all BEFORE the stream starts.
      - **Provider TTFB: 21.2s** on the actual agent round-1 request
        (`[llm-ttft] ttfb_ms=21248 ttft_ms=21250 first=thinking`).
        Prompt was 11.4k tokens with 96 tool schemas; no fallback/retry/
        cooldown lines → H5 ruled out. Provider-side (prefill/queue at Go);
        per the standing user decision, no prompt-size work. The comparison
        curl (minimal prompt) would confirm whether the 21s scales with
        prompt size or is flat Go queueing.
      - **H2 ruled out**: `ttft_ms ≈ ttfb_ms` and `first=thinking` — thinking
        streamed immediately once headers landed; no render/buffer delay.
      - H4: no repeated probes (`agent_prep` prep=0.78s total).

      Follow-up (user: the same model answers in ~5s in VS Code via
      opencode-copilot-chat): the 21.2s is NOT flat Go queueing — it is
      **prefill of our prompt**, and it recurs every turn because our
      serialized prefix is not byte-stable:
      - Size: 11,429 tokens + 96 tool schemas vs the plugin's few-KB Copilot
        chat prompt. 21s ≈ 11.4k tokens at Go's prefill rate; 5s ≈ ~2–3k
        tokens at the same rate. Prefill is proportional; no queueing anomaly.
      - **Prefix instability (app-side, fixable without trimming):**
        `_build_base_prompt` (`src/agent_loop.py`) builds the SYSTEM prompt
        from the per-turn tool-RAG selection (`relevant_tools` →
        `_assemble_prompt(tool_names, ...)`), and `_build_system_prompt`'s
        cache key includes `frozenset(relevant_tools)` — the system text
        changes turn-to-turn (log: `[tool-rag] Retrieved tools for query:
        [...]`). The system message sits first in the serialized prompt, so
        Go's prefix KV cache misses every turn and the full 11.4k prefill is
        paid every turn. The 96 tool schemas ARE stable and the
        memory/RAG/skills blocks are already late user-role messages (#2927
        work) — only the system prompt varies.
      - Verification: minimal-prompt curl (~5s expected) + replay the app's
        exact payload twice byte-identical (2nd should be much faster if Go
        caches prefixes).
      - Fix options (Phase 2B, H2/prefill branch — NOT prompt trimming):
        (A) freeze the tool-RAG tailoring — full stable tool-section prompt
        (or per-session frozen selection) so the prefix is byte-stable and
        Go's cache hits; slightly larger system prompt, prefill paid once per
        session instead of per turn. (B) keep per-turn tailoring but move the
        relevant-tools note out of the system role into a late untrusted
        user-role message (same pattern as the skill index), keeping the
        system prefix static. DECIDED 2026-11-03: **Option A** (user: "no
        tailoring of that prompt").
      DONE 2026-11-03 (user approved "Both"):
      - `_build_base_prompt` (`src/agent_loop.py`) no longer composes from the
        per-turn `relevant_tools` selection — every caller gets the full
        tool-inventory prose (non-admin keeps the historical management-
        section strip; `compact` handled inside `_assemble_prompt`). Output is
        a pure function of (disabled_tools, needs_admin, compact) → prefix
        byte-stable across turns. Also fixed while unifying: the admin
        fallback used to return the precomputed `AGENT_SYSTEM_PROMPT`, which
        IGNORED `disabled` — disabled tools' sections shipped in the prose
        for admin turns; composition is now fresh and filters them.
      - `_build_system_prompt` cache key drops `frozenset(relevant_tools)`;
        `relevant_tools` keeps its routing/schema-selection roles.
      - Tests: `tests/test_full_inventory_prompt.py` — prose identical across
        different RAG selections (admin + non-admin), disabled filtering
        (incl. the admin fix), cache-key independence, and the email alias
        expansion below. First run 2026-11-03: 410/411 — the cache-key test
        caught TWO more per-turn system-message variations in
        `_build_system_prompt`, both fixed:
        (a) the EMAIL DOCUMENT FORMAT block was gated on
        `relevant_tools ∩ _EMAIL_TOOL_HINTS` → now availability-gated
        (`_EMAIL_TOOL_HINTS - disabled`; generic how-to-format guidance, so
        stable gating is semantically right; disabling all email-capable
        tools removes it);
        (b) `_local_computer_rules()` was appended to the system prompt when
        the selection contained workspace tools → it asserts a per-turn fact
        ("the user referred to this computer"), so it KEEPS its selection
        gate but now ships as a late user-role message
        (`_local_computer_message`, inserted before the last user message
        like the datetime message) instead of mutating the system role.
        Two new tests cover both. Re-run pending. Known remaining
        occasional system-message variation (out of scope, fires only on
        specific contextual followup turns): `_prepend_agent_directive`
        callers and the native artifact/media workspace rules.

## Phase 2B — Fixes, selected by the Phase 2A finding

**User decisions (2026-10-28): prompt size is not the issue — do not check it,
and no token limiting.** No trimming, clamping, schema-surface reduction, or
prompt-size instrumentation.

- [ ] **If H2 (thinking transport):** map `thinking_mode` for `opencode-go` in
      `_apply_hosted_thinking_mode` (send the provider's disable/enable thinking
      parameter per the user's toggle), and/or make the frontend render
      `thinking=True` deltas immediately so TTFT is visibly sub-second.
- [x] **If H3 (pre-flight):** fix or parallelize the slow stage surfaced by the
      `chat_prep` timings.
      DONE 2026-11-03: agent-mode turns now skip the pre-flight web search
      entirely (`skip_web` includes `bool(agent_mode)` in
      `routes/chat_helpers.py`; the personal-tool regex special case is
      subsumed and removed). Rationale: the agent already carries
      `web_search`/`web_fetch` tools and searches in-stream, where the search
      does not delay the first token; plain chat mode (no tools) keeps the
      pre-search. Expected effect on the measured turn: ~54s → ~21s TTFT
      (the remaining 21.2s is provider TTFB, app-side unfixable without the
      ruled-out prompt-size work). Tests:
      `tests/test_chat_helpers.py::test_build_chat_context_skips_pre_web_for_every_agent_turn`
      and `..._keeps_pre_web_for_plain_chat`; the two existing agent
      calendar/notes skip tests still pass.
- [ ] **If H5 (retry/reroute chain):** eliminate the wasted pre-Go attempt —
      route straight to the session's configured endpoint, or cut the
      retry/cooldown stacking the timeline exposes.
- [ ] **If H4 (probes):** cache the catalog probe per session instead of
      per (url, model) re-validation.
- [ ] Consider extending `_first_token_timeout` watchdog coverage to public
      providers (opt-in via env) so a hung Go stream surfaces as an error
      instead of an indefinite spinner.

### Next recommended task for Task 2

Phase 2A instrumentation is implemented (2026-11-03). Remaining:
1. Execute the new tests (`tests/test_llm_ttft_logging.py`,
   `tests/test_chat_prep_timing.py`) plus the touched-path suites
   (`tests/test_llm_core_streaming.py`, `tests/test_chat_helpers.py`,
   `tests/test_kv_cache_invalidation_2927.py`, `tests/test_review_regressions.py`).
   DONE 2026-11-03: user ran them — 119/121 passed; the 2 failures were the
   `url_fetch` timing recording an empty fetch loop as an executed stage
   (fixed: `url_fetch` only records when the message contains non-YouTube
   URLs); re-run of `tests/test_chat_prep_timing.py` pending confirmation.
2. User runs `docs/TTFT_REPRO_RUNBOOK.md` on the live deployment and pastes
   the `[chat-prep-timing]` / `[llm-ttft]` / `[fallback]` lines + comparison
   curl timing back here; then select the Phase 2B fix (H2 shape is already
   known from the reference extension: GLM `thinking: {type}` control).
   DONE 2026-11-03: user pasted `test.log` — H3 confirmed and fixed (agent
   turns skip the pre-flight web search), H2/H5 ruled out, remaining 21.2s
   is prefill of a per-turn-varying prefix (see the follow-up note above;
   fix decided as Option A, deferred).
3. Implement Option A (prefix-stability / tailoring removal) when the user
   is ready — see the Phase 2B notes above.

### Side finding (2026-11-03, corrected): built-in email MCP tools cannot be disabled from the UI

The user disabled many tools (96 → 42 sent) but the 19 `mcp__email__*`
   tools still ship; the Integrations tab is empty and the user never added
   any MCP server. Verified cause chain:
   1. The email tools come from a **built-in MCP server** — `src/builtin_mcp.py`
      auto-registers `mcp_servers/email_server.py` ("Built-in: Email") at
      startup, alongside memory / rag / image_gen / builtin_browser. The user
      never configured it; it ships with the app.
   2. The Integrations tab lists only **user-added** servers (DB `McpServer`
      rows via `/api/mcp/servers`); built-ins have no DB row → they never
      appear there → the per-tool checkbox panel is unreachable for them.
   3. The Tools tab toggles write **bare** names (`send_email`) into the
      global `disabled_tools` setting, but the request ships **qualified**
      schemas (`mcp__email__send_email`), and the final schema filter in the
      agent loop (`src/agent_loop.py` ~25150) is an exact-name match — so a
      bare disable does NOT remove the qualified schema. Execution IS blocked
      (bare↔qualified aliasing in the gate), so the model sees the tool,
      calls it, and gets blocked — the worst combination.
   4. `get_all_openai_schemas` skips built-in Python servers EXCEPT
      `builtin_browser` and `email` — email is deliberately in the native
      schema surface ("omitting their schemas makes qualified email calls
      impossible to select").
   Practical disable paths today: `ODYSSEUS_DISABLE_MCP=1` (kills ALL MCP —
   too broad) or manually POSTing qualified names to `/api/tools` (works via
   exact match, but no UI offers those names).
   PROPOSED FIX → DONE 2026-11-03 (user approved "Both"):
   `_expand_disabled_email_aliases` in `src/agent_loop.py` (both directions:
   bare ↔ `mcp__email__<bare>`), applied once at `stream_agent_loop` entry
   right after `disabled_tools` normalization, so every downstream
   exact-match filter (schema surface at the tools composition, prompt prose)
   agrees with the execution gate's aliasing. A bare email disable in the
   Tools tab now removes the qualified schema AND the prose section — the
   tool disappears from the request instead of staying visible-but-blocked.
   Tests in `tests/test_full_inventory_prompt.py` (both directions, empty
   inputs, qualified-disable drops the bare prose section, source-level
   guard that the loop applies the expansion at entry). NOT yet executed.
   Not done (still optional): surfacing built-in servers in the Integrations
   list for per-tool control.

---

## Task 2 follow-up (2026-11-03): prewarm the next turn's fixed costs

User request: "make the app prepare anything it can before sending the
prompt" — at new-chat creation and after each completed response, so the
send click does as little work as possible.

**What is precomputable (message-independent):** context-window resolution
for (endpoint, model); the base agent system prompt (byte-stable since the
full-inventory change — one build fills `_cached_base_prompt` and the next
send reuses it); the MCP schema surface. **What is NOT:** tool-RAG selection,
memory recall, preprocess — they need the user's text.

**The big post-response win:** the compaction check. `maybe_compact` runs on
the send path today and includes an LLM summarization call when the history
is over the threshold. Running it on the post-response history moves that
LLM call to while-the-user-reads; the next send's check then no-ops.

- [x] `src/turn_prewarm.py`: `schedule_session_prewarm` (fire-and-forget,
      single-flight per session, `ODYSSEUS_TURN_PREWARM=0` disables) →
      `prewarm_session_turn` (context length + `_build_system_prompt` for
      the common turn shape: global disabled ∪ owner blocks ∪ email aliases,
      needs_admin=False, mcp_mgr from the manager + MCP schemas); and
      `prewarm_compaction` (maybe_compact on post-response history).
- [x] Trigger points wired: session creation + mid-session model switch
      (`routes/session_routes.py`), post-response compaction pre-check
      (`routes/chat_helpers.py` `run_post_response_tasks`) — the compaction
      job joins the SAME sequential extraction queue as memory/skill
      extraction so it never competes with the next turn (#2927); skipped
      for incognito/compare turns.
- [x] Tests: `tests/test_turn_prewarm.py` — base-prompt cache is warm and a
      real-shaped turn HITS it; compaction pre-check calls maybe_compact
      with the history; empty history skips; single-flight; env disable;
      trigger-point wiring. NOT yet executed.

Known residual send-path costs that cannot be prewarmed: tool-RAG chroma
query (~0.6s, message-dependent), memory recall, preprocess. Also noted:
`_detect_admin_intent` is message-dependent, so a turn whose text flips
admin intent builds a different (full vs mgmt-stripped) prompt — occasional
cache miss by design; if second-turn TTFT still disappoints, making
needs_admin owner-based instead of intent-based is the next lever.

### Follow-up (2026-11-03): no per-turn tool recommendations — completed

User: "a recommendation was what I said to not do." Final state on
full-surface turns (the common hosted shape):
- MCP prose catalog (`_build_system_prompt`) describes EVERY enabled MCP
  tool — no `allowed_names` filtering, no selection gate. `allowed_names`
  remains only as a compatibility parameter of
  `McpManager.get_tool_descriptions_for_prompt` (no production caller).
- The chroma similarity retrieval is SKIPPED when the primary endpoint's
  configured tool surface is "full" (`_primary_surface_full` computed via
  `_configured_model_tool_surface` before the retrieval block) — the
  selection can no longer restrict anything there, and the deterministic
  keyword fallback feeds the remaining routing heuristics. Saves the
  measured ~0.6s `tool_selection` cost. Compact/selected surfaces (local /
  small models, where the selection IS the surface because the window
  cannot hold all schemas) keep the retrieval.
- `_relevant_tools` is now purely a server-side routing hint on full
  surfaces: nothing model-visible (schemas, system prose, MCP catalog) is
  derived from it there.
Tests: `tests/test_full_inventory_prompt.py` —
`test_mcp_catalog_describes_all_enabled_mcp_tools`,
`test_full_surface_skips_similarity_retrieval` (loop-level: get_tool_index
  must not be called). NOT yet executed.

---

## Task 3 (2026-11-01): Denying an approval card must continue the run, not end it

**Symptom:** when the agent shows an "allow" card and the user clicks Deny,
execution stops entirely. Expected: the run continues without the disallowed
action (model proceeds via an alternative path or answers from evidence it
already has).

### Verified root cause

1. **Deny short-circuits the route** — `routes/chat_routes.py` (~line 2866):
   `if decision == "deny": return StreamingResponse(_tool_approval_resolution_stream(decision))`.
   That stream (`_tool_approval_resolution_stream`, ~line 1369) emits only
   `{"type": "tool_approval_resolved", "decision": "deny"}` + `[DONE]`. The
   agent loop is never resumed.
2. **Approve resumes; deny does not** — the approve branch (~line 2873+) sets
   `message = pending_tool_approval.continuation_query`, restores the sealed
   workspace/document, sets `tool_approval_continuation = True`, and re-enters
   `stream_agent_loop` with the exact admission. Deny has no analogous
   continuation.
3. **Frontend treats resolution as turn end** — `static/js/chat.js` (~line 4045):
   `tool_approval_resolved` cancels timers, destroys the spinner, removes the
   round holder, and `continue`s — nothing can render after it in the same
   stream.
4. **The model never learns of the denial** — `_mark_tool_approval_resolved`
   (~line 1310) only stamps the persisted card metadata; no denial signal or
   run-denied marker reaches the resumed conversation, so even a manual
   follow-up can't reference it and a retry would just re-prompt.

### Design

- **Deny = control-plane continuation, minus the grants.** Consume + mark
  resolved (unchanged), then resume `stream_agent_loop` with the sealed
  continuation context (workspace, document, selected tools) but:
  - NO exact admission / `ExactToolApproval` grant;
  - NO `allow_bash` / `allow_web_search` toggle restore;
  - a synthetic **system** message: the user denied `<tool>` (sealed content
    summary/digest only), do not attempt this action again this run, continue
    without it or answer from existing evidence. Fixed server-side wording —
    never echo untrusted tool content beyond a bounded digest.
- **Run-denied marker** (shared with Task 1 Phase 4): the denied operation is
  recorded for the resumed run so a retry gets the hard error without another
  card; N repeated proposals of the same operation are capped per run.
- **Hard-stop deny stays** where there is no resumable conversation or no human:
  skills tester / `job_coordinator` (`allow_continuation=False` — the test
  legitimately stops), delegated credentials (already 403 in
  `_reject_delegated_tool_approval`), unattended runs (no card is shown).
- **Frontend:** after `tool_approval_resolved`, the same stream continues with
  deltas — the card should render a "Denied" state and a new round holder must
  be created for the continued output (verify `_ensureVisibleRoundForDelta`
  and the round-holder lifecycle after the current removal logic).

### Phases

- [ ] **Phase A — Backend deny-continuation:** in `routes/chat_routes.py`, replace
      the deny short-circuit with a continuation that mirrors the approve branch's
      context restoration (workspace, document, `chat_mode = "agent"`,
      `tool_approval_continuation = True`) while skipping every grant restore;
      prepend the fixed denial system message; stream `tool_approval_resolved`
      first, then the resumed loop's events.
- [ ] **Phase B — Run-denied marker (per-run only):** record the denied
      (tool, content digest) **in memory, keyed to the run id** — the resumed
      run only. Semantics:
      - "Hard-fail" = the retried tool call returns an error result **to the
        model inside the continuing run** (same shape as existing policy
        blocks: `{"error": ..., "blocked": True, "policy": "user_denied"}`).
        The run does NOT end; the model reads the denial and continues another
        way. No second card is shown for the same operation in the same run.
      - **Reset on new prompt:** the marker is never persisted (deny already
        leaves no session signature — `stamp_chat_session_grant` in
        `src/tool_approval_scopes.py` only stamps approvals). A new user
        message starts a new run: marker gone, and the model may propose the
        same action again and get a fresh card — denial is per-prompt, since
        an action denied for one prompt may be wanted for the next.
      - Cap repeated proposals of the same operation per run.
      (This is Task 1 Phase 4 — implement once, use for both gates.)
- [ ] **Phase C — Frontend:** `static/js/chat.js` `tool_approval_resolved` handler:
      mark the card Denied instead of ending the turn; ensure subsequent deltas
      in the same stream get a fresh round holder; same check in
      `static/js/compare/stream.js` (~line 554).
- [ ] **Phase D — Tests & docs:** deny → run continues and the model receives the
      denial message; denied action retried → error result to the model, run
      continues, no second card; **new user prompt resets the marker** (same
      action may be proposed and carded again in the next run);
      approve path unchanged; skill-tester/job-coordinator deny still stops the
      test; delegated/unattended still never prompt. Update
      `docs/AGENT_TURN_CONTRACT.md` and `THREAT_MODEL.md` (deny is a
      continuation, not a termination; denial text is server-authored).

### Risks

- Model re-requests the denied action → covered by the Phase B marker; without
  it, deny-continuation would loop cards.
- Prompt injection through the denial message → fixed server wording, bounded
  digest of sealed content only.
- Frontend round-holder lifecycle after `tool_approval_resolved` — must be
  verified live before Phase C is called done.
- Interaction with Task 1 Phase 3 (task-scope widening): a deny must never be
  lifted by a later TASK-scope approval of a different action.

### Next recommended task

Task 1 Phase 2 (approval card for ungranted operations) — Phase 1 landed the
`request_authority_ungranted` marker; Phase 2 turns it into an interactive
approval instead of a hard error. (Task 3 Phase A follows after Task 1.)
