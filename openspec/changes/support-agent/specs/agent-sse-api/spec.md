## ADDED Requirements

### Requirement: Streaming chat endpoint

The system SHALL expose an HTTP endpoint that accepts a conversation and streams the agent's response as Server-Sent Events, so the client renders the answer progressively.

#### Scenario: Answer streams token-by-token
- **WHEN** a client posts a conversation to the chat endpoint
- **THEN** the server responds with an SSE stream that emits the assistant's text as incremental token events as they are generated

#### Scenario: Stateless conversation
- **WHEN** a client continues a multi-turn conversation
- **THEN** it sends the prior messages with the request and the server does not require server-side conversation storage

#### Scenario: Terminal events
- **WHEN** the agent finishes or an error occurs
- **THEN** the stream emits a terminal completion event on success, or an error event carrying a message on failure

### Requirement: Intermediate steps surfaced to the client

The stream SHALL surface intermediate agent steps — tool calls and API queries — as their own typed events, not only the final answer.

#### Scenario: Tool call is announced before it runs
- **WHEN** the agent decides to call a tool (e.g. searching the knowledge base or querying an API)
- **THEN** the stream emits a typed step event before the tool executes, carrying a human-readable label (e.g. the search query) plus the structured input

#### Scenario: Tool result is summarized to the client
- **WHEN** a tool finishes executing
- **THEN** the stream emits a typed result event with a short summary (e.g. how many articles were found) so the client can resolve the in-progress step

#### Scenario: Citations are delivered
- **WHEN** the agent answers using retrieved chunks
- **THEN** the stream conveys the `kb_faq` chunk ids/topics the answer drew on so the client can show sources

### Requirement: Structured input via UI-request widgets

The stream SHALL let the agent request structured input from the user through a typed UI-request (widget) event, and the risky value SHALL be collected from the widget rather than parsed from LLM free text.

#### Scenario: Agent requests a widget instead of parsing free text
- **WHEN** the agent needs a value that must not come from free text (e.g. a date, or a choice between options)
- **THEN** the stream emits a typed `ui_request` event carrying a widget spec (e.g. `date_picker` or a `choice`/buttons list) and a correlation id, and the turn pauses awaiting the user's selection
- **AND** the field is not present in the LLM's tool schema, so the model can trigger the widget but cannot supply the value itself

#### Scenario: Selected value returns as structured input
- **WHEN** the user makes a selection in the rendered widget
- **THEN** the selection returns as a structured value carrying the correlation id, and the agent resumes using that value verbatim

#### Scenario: Widget round-trips are free
- **WHEN** a UI-request/selection round-trip occurs
- **THEN** it does not count toward the conversation turn budget

### Requirement: Reusable agent core

The agent core SHALL be importable independently of the web layer, yielding a typed event stream that the API serializes to SSE.

#### Scenario: Core yields typed events
- **WHEN** the agent core processes a turn
- **THEN** it yields typed events (token, tool step, tool result, citations, completion, error) that the web layer serializes, so the same core can drive a CLI or an eval harness without the HTTP server
