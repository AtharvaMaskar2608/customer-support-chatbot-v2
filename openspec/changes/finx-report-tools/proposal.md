## Why

The CHO-20 support agent answers questions grounded in `kb_faq` — it can explain *how* to get a CML or a contract note, but it can't fetch one. This change gives the agent two **read-only account-data tools** that call the FinX MIS reports API, so it can pull a customer's actual **CML report** and **contract note** on demand and stream the "generating your report…" progress to the user. It lands in the HTML-frontend phase, layered on the CHO-20 agent — not folded into it.

Scope is deliberately tight: **reports only, read-only.** The FinX trading OpenAPI (orders, funds, payments, EDIS) is explicitly out of bounds — the only thing we touch from that surface is minting a JWT.

## What Changes

- Add **machine-to-machine auth** (`app/finx/auth.py`): exchange the FinX API key for an SSO JWT (`iss: sso.choiceindia.com`, ~8h) via the M2M login endpoint — no OTP, no human in the loop. Cache the JWT and refresh transparently on expiry / `401`.
- Add two **read-only Claude tools** the agent can call in its loop:
  - `get_cml_report(mobile)` → `POST /mis/v2/reports/v2/generate` `{reportType:"cml", searchBy:"mobile-number", searchValue:<mobile>}`
  - `get_contract_note(mobile, contract_date)` → `POST /mis/v2/contract-note/generate` `{mobileNo:<mobile>, contractDate:<dd-mm-yyyy>}`
  - Both send `Authorization: <jwt>`, `authType: jwt`, `source: FINX_WEB`.
- Extend the streaming so intermediate steps and the result are visible (building on CHO-20's event model): a `tool_use` step (*"Generating the CML report for 8779552825…"*), a `tool_result` summary, and a new **`report` / artifact event** carrying the report itself (PDF link / bytes / structured data — exact shape confirmed in task 1).
- Register the tools in the CHO-20 agent + a guardrail note: the agent uses these only for report requests, and says so when a report can't be produced.
- Add FinX config + deps (`httpx` or `aiohttp` for the MIS calls) in `app/`.

Non-goals (deferred to production, not this POC):
- **Identity pinning / access control.** For this POC a **human tester chooses the account** (the mobile number is a tool input). Production MUST bind the number to an authenticated session and never let the model pick it — recorded here as a hard production follow-up, not built now.
- Secrets management. POC reads the API key from `.env`; production must use a vault. (`.env` is git-ignored; the key is never logged or committed.)
- Any trading / order / funds / payment endpoint.
- The HTML frontend itself (this change delivers the tools + streaming contract the frontend consumes).

## Capabilities

### New Capabilities
- `finx-auth`: Machine-to-machine exchange of the FinX API key for a short-lived SSO JWT, with in-process caching and refresh-on-expiry, used to authorize the MIS report calls. Read/secret-safe (never logs or commits credentials).
- `account-report-tools`: Two read-only Claude tools (`get_cml_report`, `get_contract_note`) that call the FinX MIS reports API with the JWT and return the report to the agent, surfaced in the agentic loop with streamed intermediate steps and a report artifact event.

### Modified Capabilities
<!-- None in main specs. Layers on the CHO-20 `support-agent` / `agent-sse-api` capabilities, which are still in-flight (not yet archived); the new report/artifact event is specified here to stay self-contained. -->

## Impact

- **New files**: `app/finx/auth.py`, `app/finx/reports.py` (the two tools + schemas), FinX config in `app/config.py`, an `artifact`/`report` SSE event in `app/events.py`.
- **Depends on**: the CHO-20 agent loop + SSE event model; a FinX API key; the MIS endpoints.
- **Dependencies**: an async HTTP client (`httpx`) for the MIS calls (+ existing `anthropic`, `fastapi`).
- **Config/secrets**: `FINX_API_KEY` (+ login URL / any client id) from `.env`; the SSO JWT is derived at runtime, never stored in git.
- **External calls**: per report request — 1 M2M login (cached/reused) + 1 MIS report call. Read-only against FinX; no writes anywhere.
- **Known risks to confirm in task 1**: exact M2M login request/response; report response shape (PDF vs URL vs JSON); whether the API key is IP-restricted (its payload carries a `CliIPAddress`).
- **Linear**: new issue (App phase 2), layered on CHO-20; consumes CHO-17/18 indirectly via the same agent.
