## ADDED Requirements

### Requirement: Machine-to-machine JWT auth

The system SHALL obtain a FinX SSO JWT from the FinX API key via the machine-to-machine login, with no human/OTP step, and use it to authorize MIS report calls.

#### Scenario: API key is exchanged for a JWT
- **WHEN** a report tool needs to call the MIS API and no valid cached JWT exists
- **THEN** the system performs the machine-to-machine login with the configured FinX API key and obtains an SSO JWT
- **AND** MIS calls are sent with `Authorization: <jwt>`, `authType: jwt`, and `source: FINX_WEB`

#### Scenario: JWT is cached and refreshed
- **WHEN** a valid JWT is already cached
- **THEN** it is reused rather than re-logging-in on every call
- **AND** when the JWT is expired (or a call returns 401) the system re-logs-in once and retries, transparently to the agent

### Requirement: Credential safety

The system SHALL keep FinX credentials out of logs and version control.

#### Scenario: Secrets are never logged or committed
- **WHEN** the system authenticates or logs activity
- **THEN** the API key and JWT are never written to logs or committed to the repository, and the API key is read from configuration/`.env` (git-ignored)

#### Scenario: Trading endpoints are not used
- **WHEN** the FinX integration runs
- **THEN** it calls only the machine-to-machine login and the read-only MIS report endpoints, and never any trading/order/funds/payment endpoint
