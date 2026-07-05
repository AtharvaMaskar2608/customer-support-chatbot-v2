## ADDED Requirements

### Requirement: Hybrid RRF retrieval over the knowledge base

The system SHALL provide a reusable, read-only retrieval function over `kb_faq` that reuses the CHO-17 hybrid RRF recipe (dense vector + full-text + Reciprocal Rank Fusion) and the same embedding model and dimensions as `kb_faq.embedding`.

#### Scenario: Query is retrieved via the RRF path
- **WHEN** the retrieval function is called with a query string
- **THEN** it embeds the query with the same model and dimensions as `kb_faq.embedding`, runs the RRF query, and returns the top-K chunks
- **AND** it reuses the CHO-17 RRF SQL and retrieval configuration rather than reimplementing retrieval

#### Scenario: Embedding dimensionality is verified
- **WHEN** the application starts
- **THEN** it asserts the query embedding dimensionality matches `kb_faq.embedding` before serving requests

#### Scenario: Read-only against the knowledge base
- **WHEN** retrieval runs
- **THEN** it performs only read queries against `kb_faq` and never modifies the table

### Requirement: Retrieval exposed as a Claude tool

The system SHALL expose retrieval as a Claude tool named `search_knowledge_base` with a documented input schema, returning ranked chunks that carry enough identity for grounding and citation.

#### Scenario: Tool schema is defined
- **WHEN** the agent is configured
- **THEN** a `search_knowledge_base` tool is registered with an input schema accepting a `query` (and an optional result count) and a description that tells the model when to use it

#### Scenario: Tool result carries chunk identity
- **WHEN** the tool executes
- **THEN** it returns the top-K chunks each with its `kb_faq` id, topic, and text, so the agent can answer from and cite them
