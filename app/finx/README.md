# `app/finx/` — FinX account-report tools (CHO-21)

Two **read-only** account-data tools for the CHO-20 support agent, so it can fetch
a customer's actual **CML report** and **contract note** from the FinX MIS reports
API — not just explain how to get them.

- `get_cml_report(mobile)` → `POST /mis/v2/reports/v2/generate`
  `{reportType:"cml", searchBy:"mobile-number", searchValue:<mobile>}`
- `get_contract_note(mobile, contract_date)` → `POST /mis/v2/contract-note/generate`
  `{mobileNo:<mobile>, contractDate:<dd-mm-yyyy>}`

Both authenticate with an **SSO JWT** (`Authorization: <jwt>`, `authType: jwt`,
`source: FINX_WEB`), minted machine-to-machine from `FINX_API_KEY` (no OTP, no
human). The JWT is cached and refreshed transparently on expiry / `401`.

## Hard boundary — reports only, read-only

The FinX **trading** API (orders / funds / payments / EDIS) is **never called**.
The only endpoints touched are the M2M login and the two `/mis/v2` report
endpoints. This is the primary safety boundary (CHO-21 D1).

## Config (repo `.env`, git-ignored)

| Var | Purpose |
|-----|---------|
| `CHOICE_FINX_JWT` | **Dev shortcut** — a bearer JWT copied from the logged-in browser. Takes precedence when set. See "Dev testing" below. |
| `CHOICE_FINX_API_KEY` | The FINX-issued bearer JWT (`iss: FINX`). Used when no `CHOICE_FINX_JWT` is set. (`FINX_API_KEY` accepted as a fallback name.) One of these is **required** to enable the tools. |
| `FINX_AUTH_MODE` | `direct` (default — the credential *is* the bearer, sent verbatim) or `exchange` (POST it to `FINX_LOGIN_URL` to mint one). |
| `FINX_MIS_BASE` | MIS reports API base (default `https://finx.choiceindia.com`, confirmed live). |
| `CHOICE_MOBILE_NO` | Test account mobile for verification only — NOT a runtime default (the mobile is a per-request tool input; production must bind identity). |

## Dev testing with a browser token (recommended shortcut)

The IP gate below makes the machine-to-machine key unusable from a dev box. The
easy workaround: **use the SSO JWT the browser already holds.** When you log into
`finx.choiceindia.com`, the browser mints an SSO JWT *bound to your current IP*, so
a report call fired from the **same machine** matches the token's IP → no 401.

1. Log into `finx.choiceindia.com`.
2. DevTools → **Network** → click any `/mis/v2/...` request → **Request Headers →
   `Authorization`** → copy the whole token.
3. Put it in `.env` as `CHOICE_FINX_JWT=<token>` (git-ignored) and run.

The token is short-lived (~8h) and is *your* session identity — perfect for the
POC's "tester picks the account," useless for production automation (which still
needs an M2M key minted for the whitelisted server IP).

The trading-API credentials (`CHOICE_VENDOR_ID` / `CHOICE_VENDOR_KEY` /
`CHOICE_ENCRYPTION_KEY` / `CHOICE_ENCRYPTED_IV`) are **not read by this code** —
CHO-21 D1 keeps the trading OTP flow out of bounds.

## ⛔ The gate: the key is IP-bound (confirmed twice, live)

The report call returns `401 {"message":"Invalid Request."}` unless the request's
source IP (as Choice sees it) equals the `CliIPAddress` baked into the key. Two
keys were tested; both are bound to **private IPs** (`10.40.15.9`, `192.168.16.157`)
that the dev machine's NAT'd public IP does not match — hence 401.

**Live verification MUST run from the whitelisted host** — the AWS server, with a
key minted so `CliIPAddress` = the source IP Choice sees from that box. It cannot
succeed from a dev machine over the public internet. This is CHO-21 task 1.3, now
the confirmed blocker.

## The report SSE event

On success the agent emits a typed `report` event alongside the `tool_use` /
`tool_result` steps (see `../README.md` for the full contract). It is
**shape-agnostic** so both report types use it:

```
event: report
data: {"report_type":"cml","summary":"Your CML report is ready.","url":"…"}
```

`parse_report_response()` maps whatever the MIS API returns into it: raw PDF bytes
→ `content_b64` (+ `mime`/`filename`), a JSON download link → `url`, or a JSON
body → `payload`.

## CHO-21 task 1 — what a live probe confirmed, and what's still open

A single read-only probe was run against the real endpoint. Findings:

**Confirmed:**
- The `FINX_API_KEY` is itself an **RS256 JWT** (`iss: FINX`, ~30-day expiry) whose
  payload carries `UserId`, `CliIPAddress`, `sub`, `exp`. So the "API key" and the
  bearer token are one and the same artifact.
- `POST https://finx.choiceindia.com/mis/v2/reports/v2/generate` is **live and
  correct** — it returns a structured FinX envelope
  `{"statusCode":…, "message":…, "devMessage":…, "body":{…}}`. Success data rides
  under **`body`**; both parsers now read that envelope.
- **The key is IP-bound and the API enforces it.** `CliIPAddress` is `10.40.15.9`
  (a private/internal address). Calls that don't originate from that IP return
  `401 {"message":"Invalid Request."}`. Live verification therefore MUST run from
  the deployment host at `10.40.15.9` (inside Choice's network) — it cannot succeed
  from an external egress IP. (This is CHO-21 task 1.3, now confirmed as the gate.)

**Still open (needs a run from `10.40.15.9`):**
1. **Direct-JWT vs. exchange.** From the wrong IP every auth path returns 401, so
   we can't yet tell whether the FINX key is used *directly* as the MIS
   `Authorization` (what `reports.py` assumes today, bypassing login) or must be
   *exchanged* for an `sso.choiceindia.com` token first. Run the probe from the
   correct IP; if the direct call 200s, `FinxAuth` is not needed on the read path
   (keep it only if an exchange is required). `auth.py` is ready for the exchange
   path and reads expiry from the JWT's own `exp` regardless.
2. **Report response body shape** — PDF bytes vs a `body.url` vs a `body` JSON
   payload, and sync vs poll-for-completion. `parse_report_response` handles the
   first three; if it is **async/poll**, add a server-side poll loop in
   `reports.py` (the streamed "generating…" step already covers the UX wait, D5).

## 🔒 Production follow-ups (POC shortcuts — must NOT ship as-is)

- **Identity / IDOR.** For this POC a human tester chooses the account, so
  `mobile` is a tool input. **Production MUST bind the number to the
  authenticated customer and refuse arbitrary numbers** — the model must never
  pick whose report to fetch. (CHO-21 D7.)
- **Secrets.** The POC reads `FINX_API_KEY` from `.env`; production must use a
  secret vault. The key/JWT are never logged or committed, but the sample tokens
  shared during exploration should be rotated regardless.
