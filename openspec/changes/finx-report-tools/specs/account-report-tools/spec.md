## ADDED Requirements

### Requirement: CML report tool

The agent SHALL have a read-only tool that fetches a customer's CML report from the FinX MIS reports API.

#### Scenario: CML report is fetched by mobile number
- **WHEN** the agent calls the CML report tool with a mobile number
- **THEN** the system calls `POST /mis/v2/reports/v2/generate` with `{reportType:"cml", searchBy:"mobile-number", searchValue:<mobile>}` and the authorized JWT headers
- **AND** it returns the report result to the agent

### Requirement: Contract-note tool

The agent SHALL have a read-only tool that fetches a customer's contract note for a given date.

#### Scenario: Contract note is fetched by mobile number and date
- **WHEN** the agent calls the contract-note tool with a mobile number and a contract date
- **THEN** the system calls `POST /mis/v2/contract-note/generate` with `{mobileNo:<mobile>, contractDate:<dd-mm-yyyy>}` and the authorized JWT headers
- **AND** it returns the contract note result to the agent

#### Scenario: Contract date comes from a date widget, not free text
- **WHEN** a contract note is requested
- **THEN** the `contract_date` field is not present in the LLM tool schema; the harness requests it via a `date_picker` widget (`dd-mm-yyyy`, no future dates) and injects the user-selected value into the call
- **AND** the LLM never parses or generates the date

### Requirement: Read-only, tester-supplied account (POC)

The report tools SHALL be read-only and, for the POC, SHALL accept the target mobile number as a tool input supplied by the human tester.

#### Scenario: Reports never modify FinX state
- **WHEN** either report tool runs
- **THEN** it performs only a read/generate call and never modifies any FinX data

#### Scenario: POC account selection is recorded as a production gap
- **WHEN** the POC runs
- **THEN** the mobile number is taken as a tool input (tester-chosen), and the design records that production MUST bind the account to an authenticated identity rather than accept an arbitrary number

### Requirement: Intermediate steps and report delivery are streamed

The agent SHALL surface each report tool call as a streamed intermediate step and deliver the resulting report to the client as a typed artifact event.

#### Scenario: Report generation is announced
- **WHEN** the agent decides to fetch a report
- **THEN** the stream emits a typed step event before the call runs, with a human-readable label (e.g. "Generating the CML report for <mobile>…")

#### Scenario: Report is delivered as an artifact event
- **WHEN** a report call completes
- **THEN** the stream emits a typed report/artifact event carrying the report (a link, payload, or summary) so the client can present it, plus a short result summary
