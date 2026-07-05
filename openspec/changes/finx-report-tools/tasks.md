## 1. Confirm the unknowns (do first)

> Blocked: needs a live `FINX_API_KEY` + network access to FinX (neither is present
> in this environment). The code is written defensively against the documented
> shapes so confirming these is a config/edit, not a rewrite — see `app/finx/README.md`.

- [ ] 1.1 Get the exact **M2M login** call: URL, where the API key goes (`Authorization: Bearer` vs body field), and the response JWT + expiry field names (sample curl / request+response)
- [ ] 1.2 Inspect one real **report response** for shape (PDF bytes / download URL / JSON; sync vs poll-for-completion) using the test user
- [ ] 1.3 Verify the API key works from the deployment's egress IP (the key payload carries `CliIPAddress`)

## 2. FinX auth

- [x] 2.1 `app/finx/auth.py`: `FinxAuth` that exchanges `FINX_API_KEY` for the SSO JWT via the M2M login; cache JWT + expiry (expiry read from the JWT's own `exp` claim, robust to task-1 field names)
- [x] 2.2 Refresh transparently on expiry or `401`, retry once; send `Authorization: <jwt>`, `authType: jwt`, `source: FINX_WEB` on MIS calls
- [x] 2.3 Never log/commit the key or JWT; read `FINX_API_KEY` (+ login URL) from `.env`

## 3. Report tools

- [x] 3.1 `get_cml_report(mobile)` → `POST /mis/v2/reports/v2/generate` `{reportType:"cml", searchBy:"mobile-number", searchValue}`; Claude tool schema + when-to-use description
- [x] 3.2 `get_contract_note(mobile)` → `POST /mis/v2/contract-note/generate` `{mobileNo, contractDate}` (dd-mm-yyyy); tool schema exposes ONLY `mobile` to the LLM — `contract_date` is harness-collected via CHO-20's `date_picker` widget (Model 2), never LLM free text; harness injects the picked value
- [x] 3.3 Parse the report response into what the tool returns to the agent (per task 1.2 shape) — shape-agnostic `parse_report_response` (PDF bytes / URL / JSON payload)

## 4. Wire into the CHO-20 agent + streaming

> The CHO-20 substrate (`support-agent`) was unbuilt, so a minimal real version was
> built alongside: `agent.py`, `events.py`, `retrieval.py`, `server.py`. The two
> report tools register into it.

- [x] 4.1 Register both tools in the agent loop alongside `search_knowledge_base` (read-only allowlist; no trading endpoints; registered only when `FINX_API_KEY` is set)
- [x] 4.2 Emit a `tool_use` step before each report call (e.g. "Generating the CML report for <mobile>…"; mobile masked in the label)
- [x] 4.3 Add a `report`/artifact SSE event to `app/events.py` and emit the report (link/payload/summary) on completion; `tool_result` summary too
- [x] 4.4 Guardrail note: agent uses these only for report requests; says so clearly when a report can't be produced (system prompt + user-safe `ReportToolError` messages)

## 5. Verify

- [x] 5.1 End-to-end with a stubbed model + stubbed FinX (unit test): ask for a CML report → "generating…" step → `report` artifact event. **Live run with the test user is blocked on `FINX_API_KEY`.**
- [x] 5.2 Contract-note happy path (unit test): request → `date_picker` widget appears → user picks → note returned; asserts the date is never in the LLM tool schema and the tool is never called without a picked date
- [x] 5.3 JWT-expiry path (unit test): stale token + a `401` → confirms one transparent re-login + retry
- [x] 5.4 `app/finx/README.md`: run notes, the SSE report event contract, and the **production follow-ups** (identity pinning, secret vault) flagged explicitly
