## Context

The CHO-20 support agent is a streaming Claude tool-use loop grounded in `kb_faq`. This change adds account-data tools on top: two read-only FinX MIS report pulls (CML, contract note). Exploration (in the `/opsx:explore` session that produced this change) established the important facts:

- The **login OpenAPI** at `finx.choiceindia.com/api/OpenAPI/Info` documents the **trading** platform (orders/holdings/funds/payments) with a `SessionId` + `VendorId/VendorKey` + **OTP** auth flow. That is **not** what the report endpoints use, and we do **not** integrate it.
- The two target endpoints live under `/mis/v2/...` and authenticate with an **SSO JWT** (`iss: sso.choiceindia.com`, ~8h) plus `authType: jwt` and `source: FINX_WEB` headers.
- There is a **machine-to-machine login** (no OTP / no human) that mints that JWT from the FinX API key. Its exact request/response is the one open input, confirmed in task 1.
- This is a **POC**: a human tester picks which account to query, so the mobile number is a tool input. Production hardening (identity binding, secret vault) is explicitly deferred.

## Goals / Non-Goals

**Goals:**
- Let the agent fetch a customer's CML report and contract note via read-only MIS calls.
- Mint + cache + refresh the SSO JWT via the M2M login, transparently to the agent.
- Stream the intermediate step ("generating report…") and deliver the report to the client as an artifact event.
- Reuse the CHO-20 agent loop and SSE event model; keep the two tools self-contained.

**Non-Goals:**
- Identity/access control (POC: tester chooses the account). Production must pin identity.
- Any trading / order / funds / payment endpoint.
- Production secret management (POC uses `.env`).
- Building the HTML frontend (this delivers the tools + streaming contract it consumes).

## Decisions

**D1 — Reports-only, read-only; trading API excluded.**
The only thing used from the trading OpenAPI is JWT minting (and even that via the separate M2M login, not the OTP flow). No mutating endpoint is ever exposed as a tool. The tool allowlist is exactly two read calls. This is the primary safety boundary.

**D2 — M2M auth: exchange API key → cached SSO JWT, refresh on expiry/401.**
`app/finx/auth.py` holds a single `FinxAuth` that, on first use, calls the M2M login with the `FINX_API_KEY` and stores the returned JWT + expiry. Every MIS call sends `Authorization: <jwt>`, `authType: jwt`, `source: FINX_WEB`. On a `401` (or when the cached JWT is within a small skew of expiry), it re-logs-in once and retries. The exact login request (URL, where the key goes, JWT/expiry field names) is **confirmed in task 1** — the design doesn't hinge on it; only the auth implementation does.

**D3 — Two tools; mobile is an argument, the contract-note date is a WIDGET (not free text).**
`get_cml_report(mobile)` → `/mis/v2/reports/v2/generate` `{reportType:"cml", searchBy:"mobile-number", searchValue}` (no date). `get_contract_note(mobile, contract_date)` → `/mis/v2/contract-note/generate` `{mobileNo, contractDate}` (dd-mm-yyyy). The **mobile** number is a tool argument (tester supplies it — D7 records production must pin identity). The **date is different**: it must never come from LLM free text (`01-07` is ambiguous, "last Friday" needs a calendar, the model can hallucinate). Per CHO-20's D9, we use **Model 2 (harness-enforced)**: `contract_date` is **removed from the LLM tool schema** — `get_contract_note` exposes only `mobile` to the model. When the model triggers it, the harness emits a CHO-20 `ui_request` `date_picker` (format `dd-mm-yyyy`, `max: today`), the user taps a date, and the harness injects that value into the MIS call. The date picker also kills the dd-mm/mm-dd ambiguity at the source. The account stays a plain argument (not a widget) — scoped to dates only.

**D4 — Report delivery as a streamed artifact event.**
The MIS report is likely a PDF (CML / contract notes usually are); it could be a URL, base64 bytes, or structured JSON — confirmed in task 1. The agent loop emits: a `tool_use` step (*"Generating the CML report for <mobile>…"* — this satisfies the "show the API-query step" requirement), a `tool_result` summary, and a new **`report`/artifact SSE event** carrying whatever the endpoint returns (a link the client can open, or a summary + payload). The event is generic so both report types use it.

**D5 — Latency is legible via the stream.**
Report generation can be slow, and may even be async (a job id to poll — confirmed in task 1). Either way the streamed "generating…" step turns the wait into visible progress rather than a dead pause. If it's poll-based, the tool loops server-side and can emit periodic status.

**D6 — Layer on CHO-20, don't fork it.**
The tools register into the existing agent loop and reuse the existing typed event stream; this change only *adds* the two tools, the FinX auth, and one new event type. No change to the KB retrieval tool or the core loop shape.

**D7 — POC shortcuts, recorded as production follow-ups.**
Two shortcuts are deliberate and must not silently become production: (a) the tester/model supplies the mobile number — production must bind it to the authenticated customer and refuse others; (b) the API key lives in `.env` — production must use a secret vault. Both are written here so they surface at the production hardening pass.

## Risks / Trade-offs

- **Exact M2M login unknown until task 1.** → Task 1 confirms the request/response with a sample curl before the auth code is written; everything downstream is already specced.
- **Report response shape unknown (PDF / URL / JSON).** → Task 1 inspects one real response; the artifact event (D4) is shape-agnostic so it absorbs whichever it is.
- **API key may be IP-restricted.** → Its JWT payload carries a `CliIPAddress`; a server with a different egress IP could get rejected. Verify against the test user early (task 1), before building the tools.
- **JWT expiry mid-conversation.** → Cache + refresh-on-401 (D2) handles it transparently; the agent never sees the auth churn.
- **POC identity gap (IDOR).** → Accepted for the POC (tester-driven); recorded as a hard production follow-up (D7). Not a bug now, but must not ship to real customers as-is.
- **Report latency / async.** → Streamed status (D5) covers UX; if async, the tool polls server-side.
- **Secrets exposure.** → `.env` only, git-ignored, never logged; the sample tokens shared during exploration should be rotated regardless.

## Open Questions

- **M2M login request/response** — exact URL, where the API key goes (`Authorization: Bearer` vs body field), the JWT field name and expiry field. (Task 1.)
- **Report response shape** — PDF bytes, a download URL, or JSON; sync vs a poll-for-completion job. (Task 1.)
- **API-key IP binding** — is the key usable from the deployment's egress IP? (Task 1.)
