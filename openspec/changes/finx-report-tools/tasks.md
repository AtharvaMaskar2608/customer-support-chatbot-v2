## 1. Confirm the unknowns (do first)

- [ ] 1.1 Get the exact **M2M login** call: URL, where the API key goes (`Authorization: Bearer` vs body field), and the response JWT + expiry field names (sample curl / request+response)
- [ ] 1.2 Inspect one real **report response** for shape (PDF bytes / download URL / JSON; sync vs poll-for-completion) using the test user
- [ ] 1.3 Verify the API key works from the deployment's egress IP (the key payload carries `CliIPAddress`)

## 2. FinX auth

- [ ] 2.1 `app/finx/auth.py`: `FinxAuth` that exchanges `FINX_API_KEY` for the SSO JWT via the M2M login; cache JWT + expiry
- [ ] 2.2 Refresh transparently on expiry or `401`, retry once; send `Authorization: <jwt>`, `authType: jwt`, `source: FINX_WEB` on MIS calls
- [ ] 2.3 Never log/commit the key or JWT; read `FINX_API_KEY` (+ login URL) from `.env`

## 3. Report tools

- [ ] 3.1 `get_cml_report(mobile)` → `POST /mis/v2/reports/v2/generate` `{reportType:"cml", searchBy:"mobile-number", searchValue}`; Claude tool schema + when-to-use description
- [ ] 3.2 `get_contract_note(mobile)` → `POST /mis/v2/contract-note/generate` `{mobileNo, contractDate}` (dd-mm-yyyy); tool schema exposes ONLY `mobile` to the LLM — `contract_date` is harness-collected via CHO-20's `date_picker` widget (Model 2), never LLM free text; harness injects the picked value
- [ ] 3.3 Parse the report response into what the tool returns to the agent (per task 1.2 shape)

## 4. Wire into the CHO-20 agent + streaming

- [ ] 4.1 Register both tools in the agent loop alongside `search_knowledge_base` (read-only allowlist; no trading endpoints)
- [ ] 4.2 Emit a `tool_use` step before each report call (e.g. "Generating the CML report for <mobile>…")
- [ ] 4.3 Add a `report`/artifact SSE event to `app/events.py` and emit the report (link/payload/summary) on completion; `tool_result` summary too
- [ ] 4.4 Guardrail note: agent uses these only for report requests; says so clearly when a report can't be produced

## 5. Verify

- [ ] 5.1 End-to-end with the test user (Ajay Kumar): ask for a CML report → see the "generating…" step → receive the report artifact
- [ ] 5.2 Contract-note happy path: request → date_picker widget appears → user picks → note returned (confirm the LLM never emits a date)
- [ ] 5.3 JWT-expiry path: force a stale token → confirm one transparent re-login + retry
- [ ] 5.4 `app/finx/README.md`: run notes, the SSE report event contract, and the **production follow-ups** (identity pinning, secret vault) flagged explicitly
