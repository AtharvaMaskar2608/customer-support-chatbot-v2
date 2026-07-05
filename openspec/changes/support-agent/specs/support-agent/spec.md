## ADDED Requirements

### Requirement: Agentic tool-use loop

The support agent SHALL run a Claude tool-use loop that lets the model call `search_knowledge_base`, feeds tool results back, and repeats until the model completes its answer.

#### Scenario: Model calls the retrieval tool and continues
- **WHEN** the model responds with a tool-use request during a turn
- **THEN** the agent executes the requested tool, appends the tool result to the conversation, and calls the model again
- **AND** the loop ends when the model stops requesting tools (its turn ends naturally)

#### Scenario: Configurable model with thinking disabled
- **WHEN** the agent is constructed
- **THEN** it uses `claude-sonnet-4-6` by default (overridable by configuration) with extended thinking disabled (`thinking={"type":"disabled"}`), so each assistant turn is a clean text + tool-use transcript replayed verbatim across the loop

#### Scenario: Static prompt is cached
- **WHEN** the agent builds a request
- **THEN** the stable instruction block (identity, static context, examples, guardrails) is marked for prompt caching and no per-request data is interpolated into it

### Requirement: Grounded, on-topic support answers

The agent SHALL answer Choice FinX brokerage support questions grounded in retrieved knowledge-base content, stay on topic, and cite the chunks it used.

#### Scenario: Product question is answered from the knowledge base
- **WHEN** a user asks a product/support question
- **THEN** the agent retrieves relevant chunks and answers grounded in them, citing the `kb_faq` chunk ids it relied on

#### Scenario: Knowledge base lacks an answer
- **WHEN** retrieval returns nothing relevant to the question
- **THEN** the agent says it does not have that information rather than inventing an answer, and points the user to an appropriate next step

#### Scenario: Off-topic question is redirected
- **WHEN** a user asks something outside Choice FinX brokerage/trading/demat support
- **THEN** the agent declines and steers the conversation back to what it can help with

### Requirement: Support guardrails

The agent SHALL enforce support guardrails: no personalized financial/tax/legal advice, no invented policy, and escalation for account-specific or sensitive actions.

#### Scenario: Advice or account action is requested
- **WHEN** a user asks for personalized financial/tax/legal advice or an account-specific/sensitive action
- **THEN** the agent does not fabricate a decision or policy and instead routes the user to a human or official channel

### Requirement: Clarifying follow-up cap

When a request is unclear, the agent SHALL ask at most two clarifying follow-ups per unclear request, resetting the count whenever the user asks something new, and then offer escalation.

#### Scenario: Up to two follow-ups per unclear request
- **WHEN** the user's request is unclear
- **THEN** the agent asks at most two clarifying follow-ups for that request
- **AND** the follow-up count resets when the user asks something new

#### Scenario: Escalation offered after the cap
- **WHEN** two clarifying follow-ups have not resolved the request
- **THEN** the agent stops asking and offers to connect the user to a human, and the user decides whether to escalate

### Requirement: Conversation length cap

The agent SHALL cap the conversation at ten substantive exchanges, enforced by the harness (not the prompt), and wind down gracefully at the cap.

#### Scenario: Ten substantive exchanges maximum
- **WHEN** the conversation reaches ten substantive user-ask/answer exchanges
- **THEN** the agent gives a graceful wrap-up and offers to connect the user to a human rather than cutting off abruptly

#### Scenario: Widgets and follow-ups do not consume the budget
- **WHEN** a widget round-trip or a clarifying follow-up occurs
- **THEN** it does not count toward the ten-exchange budget

#### Scenario: Caps are enforced in the harness
- **WHEN** the caps are applied
- **THEN** they are enforced by explicit counters in the agent loop, not by relying on the model to count its own turns
