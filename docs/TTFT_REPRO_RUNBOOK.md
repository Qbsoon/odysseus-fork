# TTFT Reproduction Runbook — OpenCode Go ~60s time-to-first-token (Task 2, Phase 2A)

**Symptom under investigation:** the app usually takes ~60s to the first visible
token with OpenCode Go (`https://opencode.ai/zen/go/v1`), while the same model in
VS Code (via the `opencode-copilot-chat` extension) responds immediately. Same
endpoint, same model → the minute is attributable to the app's request path.

**This runbook is executed on the user's live deployment** — this workspace is
development-only and cannot run the app or reach the provider live. One paste of
the captured log lines attributes the minute to one of the ranked hypotheses.

## What was instrumented (Phase 2A)

All new log lines run **always at INFO** — no env flag needed.

| Signal | Where | Meaning |
| --- | --- | --- |
| `[chat-prep-timing]` | app log, one line per turn | `build_chat_context` stage timings: `preprocess`, `history_persist`, `preface`, `memory`, `rag`, `web_search`, `url_fetch`, `skills`, `compaction`, `total` (seconds). Absent key = stage skipped. |
| `chat_prep` SSE event | browser network tab / server stream | Same timings as above, emitted as `{"type": "chat_prep", "data": {...}}` before any model output. |
| `[agent-timing] prep_done` | app log (agent mode) | Existing agent-loop prep timings (`request_setup`, `tool_selection`, `prompt_build`, `context_trim`). |
| `[llm-ttft]` | app log, one line per stream | `ttfb_ms` (request sent → response headers) and `ttft_ms` (request sent → first *meaningful* SSE frame), plus `first=` classification: `thinking` / `tool_calls` / `content` / `error`, and the first frame's delta keys. |
| `[llm-ttft] ... failed_before_first_event=` | app log | The stream died before any meaningful frame (`connect` / `read_timeout` / other), with elapsed ms — this is where a retry/reroute/cooldown stall shows up. |
| `[llm-first-token-frame]` | app log (needs `ODYSSEUS_DEBUG_LLM_SHAPE=1`) | Existing raw first-frame shape debug. |
| `[llm-shape]` | app log (needs `ODYSSEUS_DEBUG_LLM_SHAPE=1`) | Existing redacted request fingerprint. |
| `[fallback]` | app log | Existing warnings when a candidate fails before output and the chain moves to the next one — a reroute before OpenCode Go shows here. |

## Step 1 — Capture one agent-mode turn

1. Set `ODYSSEUS_DEBUG_LLM_SHAPE=1` in the deployment environment (optional but
   recommended; the new lines log regardless).
2. Send **one** agent-mode message to an OpenCode Go (GLM-5.3) session. Note the
   wall-clock time you press send and the wall-clock time the first text/thinking
   appears.
3. Capture the app log from that window and paste everything matching:

   ```text
   [chat-prep-timing]
   [agent-timing]
   [llm-shape]
   [llm-ttft]
   [llm-first-token-frame]
   [fallback]
   ```

## Step 2 — Comparison curl (same model, minimal prompt)

Run on the deployment host, with the same API key the app uses:

```bash
time curl -sN https://opencode.ai/zen/go/v1/chat/completions \
  -H "Authorization: Bearer $OPENCODE_API_KEY" \
  -H "Content-Type: application/json" \
  -H "x-opencode-session: ttft-probe" \
  -d '{
    "model": "<the same GLM model id the session uses>",
    "stream": true,
    "messages": [{"role": "user", "content": "Say hi."}]
  }' | head -c 2000
```

Note the time to the first SSE frame in the curl output. This isolates the
provider from the app entirely.

## Step 3 — Attribution

| Observation | Hypothesis | Next step (Phase 2B) |
| --- | --- | --- |
| `[chat-prep-timing] total` is tens of seconds; one sub-stage dominates (e.g. `web_search`, `rag`, `memory`) | **H3 — pre-flight** | Fix/parallelize the slow stage. |
| `[llm-ttft]` shows `ttfb_ms` small but `ttft_ms` ≈ 60s, `first=thinking` (or the first frame only arrives after ~60s) | **H2 — thinking transport** | Map `thinking_mode` for `opencode-go` (the reference extension sends GLM a `thinking: {type}` control on the same chat-completions transport) and/or render thinking deltas immediately. |
| `[llm-ttft] failed_before_first_event=` or `[fallback]` lines appear before the Go request, or the comparison curl is fast while the app's `ttfb_ms` is ~60s | **H5 — retry/reroute chain** | Route straight to the session's configured endpoint / cut retry-cooldown stacking. |
| Everything above is fast but the UI spinner still runs long | **H2 render side** | Check the browser network tab: time of `chat_prep` → first `delta`/thinking event. |
| `[agent-timing] prep_done` dominates | **H4 — probes** (downgraded) | Only if the log shows repeated catalog probes. |

## Reference: the fast VS Code path

The `opencode-copilot-chat` extension (the user's fast comparison path) does, per
its provider architecture doc:

- Routes GLM (and most models) over the **same** `/zen/go/v1` chat-completions
  transport the app uses — transport is not the difference.
- Sends an explicit per-family **thinking control**: GLM maps to
  `thinking: {type}` in the payload. The app currently sends nothing for
  `opencode-go` (`_apply_hosted_thinking_mode` only handles OpenRouter and
  Kimi), so GLM-5.3 runs with provider-default thinking.
- Captures `reasoning_content` during streaming and echoes it back on
  assistant history for GLM (tolerated family).

If Step 3 points at H2, the Phase 2B fix is to mirror the GLM thinking control
(and optionally the `reasoning_content` echo) in `_apply_hosted_thinking_mode`.
