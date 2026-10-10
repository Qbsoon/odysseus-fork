# OpenCode Provider Shape

Last updated: dev@2e2bb52 | 2026-10-10

## Scope

Canonical provider identity `opencode` with Zen/Go endpoint variants; OpenAI-compatible transport and webhook presets in `src/llm_core.py` and canonical `routes/webhook/webhook_routes.py`, with the top-level route module retained as a compatibility shim.

## Shape

Keep Zen and Go path identity in endpoint metadata even though the canonical
provider family is OpenCode. Model discovery uses general identity-only
fallback. Path/version, account policy, and model selection can differ between
variants; do not flatten them into OpenAI.

## Session Header

OpenCode **Go** requires an `x-opencode-session` header on chat requests for
routing and prompt caching; without it the Go API returns HTTP 400 ("Request
is missing x-opencode-session"). OpenCode Zen does not use this header.
`_provider_headers` in `src/llm_core.py` applies it only for the
`opencode-go` provider, using the caller's Odysseus session id when one is
passed and a process-stable fallback id otherwise (session-less calls such
as background summaries or title generation). The header is set with
`setdefault`, so an explicit value in stored session headers always wins.

## Fallback And Current Gaps

Exact `*.opencode.ai` plus configured `/zen` or `/zen/go` selects this family.
The Go `/v1/models` catalog lists only `id`/`created`/`owned_by` — no context
window fields — so Go model ids are pinned in the app-wide
`KNOWN_CONTEXT_WINDOWS` table (OpenCode-published tiers where documented,
`DEFAULT_CONTEXT` floor for GLM-5.x and MiMo-V2.6) until the catalog reports
real values. How unknown windows are budgeted is app-wide behavior, documented
with the context budget, not here.
No provider-specific rich capability catalog is mapped, and runtime still has
separate variant labels that should eventually become structured endpoint
metadata.
