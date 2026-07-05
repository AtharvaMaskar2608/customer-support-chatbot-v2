## ADDED Requirements

### Requirement: RRF-only hybrid retrieval over kb_faq

The harness SHALL retrieve from the `kb_faq` table by fusing a dense vector arm and a full-text arm using Reciprocal Rank Fusion, with no re-ranking stage.

#### Scenario: Vector and full-text arms are fused by RRF
- **WHEN** the harness executes a retrieval for a query
- **THEN** it runs a dense vector arm (`embedding <=> query_vector`, cosine) and a full-text arm (`tsv @@ plainto_tsquery`) each limited to the candidate depth N
- **AND** it fuses the two ranked lists with RRF score `1/(k + rank)` summed across arms (k = 60)
- **AND** it returns the top-K fused results with no cross-encoder or re-ranker applied

#### Scenario: Query embedding matches the column recipe
- **WHEN** the harness embeds a query for the vector arm
- **THEN** it uses `text-embedding-3-large` at `dimensions=1536` (matching the `vector(1536)` column)
- **AND** it asserts the query vector dimensionality is 1536 before querying

### Requirement: Isolate pure retrieval latency from embedding latency

The harness SHALL measure retrieval-infrastructure latency separately from query-embedding latency by embedding the query set once and reusing the vectors.

#### Scenario: Pure retrieval pass reuses cached vectors
- **WHEN** the pure-retrieval pass runs
- **THEN** the query set is embedded once and the resulting vectors are cached
- **AND** the SQL path is timed repeatedly using the cached vectors, so embedding time is excluded

#### Scenario: Arms are measured separately and combined
- **WHEN** the pure-retrieval pass runs
- **THEN** it reports latency for the vector-only arm, the full-text-only arm, and the combined RRF query independently

#### Scenario: End-to-end pass includes live embedding
- **WHEN** the end-to-end pass runs
- **THEN** it times query embedding plus RRF retrieval together
- **AND** the results make the embedding share of total latency evident

### Requirement: Realistic query set

The harness SHALL use realistic queries so full-text matches are genuine rather than empty.

#### Scenario: Queries sampled from real content
- **WHEN** the benchmark selects its query set
- **THEN** it samples queries from real `kb_faq.question` values
- **AND** it records the token-length profile of the query set

### Requirement: Record the executed query plan

The harness SHALL capture the actual Postgres query plan so the timed path is documented, not assumed.

#### Scenario: EXPLAIN ANALYZE captured before timing
- **WHEN** the benchmark starts
- **THEN** it runs `EXPLAIN ANALYZE` on the vector and RRF queries and saves the plan text with the results
- **AND** the recorded plan shows whether the vector arm used an index scan or a sequential scan

### Requirement: Latency and throughput metrics

For each measured pass and arm the harness SHALL report latency percentiles and throughput.

#### Scenario: Percentiles reported per measured unit
- **WHEN** a pass/arm completes
- **THEN** the result includes latency `p50`, `p95`, `p99`, `mean`, and `max` in milliseconds
- **AND** it includes throughput in queries per second
- **AND** it includes the corpus row count and the top-K returned

#### Scenario: Optional concurrency measurement
- **WHEN** the concurrency pass is enabled
- **THEN** the harness runs the RRF query at concurrency levels 1, 10, and 25 and reports latency + throughput per level

### Requirement: Structured, reproducible results output

The harness SHALL persist results in a machine-readable form and present a human-readable summary, read-only against the KB.

#### Scenario: Results persisted and summarized
- **WHEN** the benchmark finishes
- **THEN** it writes per-stage records to a timestamped file under `evals/retrieval/results/`
- **AND** it prints a comparison table across passes/arms
- **AND** it emits a stage-breakdown plot

#### Scenario: Read-only against the knowledge base
- **WHEN** the benchmark runs
- **THEN** it performs only read queries against `kb_faq` and never modifies the table
