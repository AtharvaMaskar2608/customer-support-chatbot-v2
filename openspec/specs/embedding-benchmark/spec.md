# embedding-benchmark

## Purpose

Benchmark OpenAI embedding models across model, concurrency, and output-dimension configurations to measure raw (uncached) latency, throughput, reliability, and cost, and persist reproducible, machine-readable results.

## Requirements

### Requirement: Benchmark grid over model, concurrency, and dimensions

The harness SHALL benchmark OpenAI `text-embedding-3-small` and `text-embedding-3-large` across a configurable grid of concurrency levels and (for `large`) output dimensions, producing one result record per `(model, dimensions, concurrency)` configuration.

#### Scenario: Default grid is exercised
- **WHEN** the benchmark runs with default settings
- **THEN** it evaluates both models across concurrency levels `1, 5, 10, 25, 50`
- **AND** it evaluates `text-embedding-3-large` at output dimensions `3072, 1536, 256`
- **AND** it evaluates `text-embedding-3-small` at its native dimension only
- **AND** it emits exactly one result record per `(model, dimensions, concurrency)` configuration

#### Scenario: Grid is configurable
- **WHEN** the operator overrides concurrency levels or dimensions via configuration or CLI flags
- **THEN** the harness uses the provided values instead of the defaults

### Requirement: Raw uncached measurement

The harness SHALL ensure measured latency reflects the raw model, uncontaminated by response caching, by embedding unique text on every call and by excluding connection cold-start.

#### Scenario: Every call embeds unique input
- **WHEN** the harness issues embedding requests
- **THEN** each request's input text is made unique via a per-call nonce so no two calls embed identical text

#### Scenario: Warmup excluded, keep-alive retained
- **WHEN** a configuration begins
- **THEN** the harness issues warmup calls whose timings are discarded
- **AND** HTTP keep-alive remains enabled so connection warmth (not response caching) is reflected

### Requirement: Concurrency execution and load metrics

The harness SHALL execute requests concurrently up to the target concurrency level for each configuration and measure behavior under that parallel load.

#### Scenario: Requests run in parallel at the target level
- **WHEN** a configuration specifies concurrency N
- **THEN** the harness keeps up to N requests in flight simultaneously
- **AND** it times each individual request

#### Scenario: Sufficient trials for stable tails
- **WHEN** a configuration runs
- **THEN** the harness records at least ~100 timed trials (after warmup) so p95/p99 are stable

### Requirement: Latency, throughput, and reliability metrics

For each configuration the harness SHALL report latency percentiles, throughput, and reliability counters.

#### Scenario: Metrics are reported per configuration
- **WHEN** a configuration completes
- **THEN** the result includes latency `p50`, `p95`, `p99`, `mean`, and `max` in milliseconds
- **AND** it includes throughput in embeddings per second
- **AND** it includes error rate, timeout rate, and count of HTTP 429 (rate-limit) responses

#### Scenario: Rate limiting is recorded, not hidden
- **WHEN** the API returns HTTP 429 responses under load
- **THEN** the harness records the 429 count as a measured output rather than failing the run

### Requirement: Cost computation

The harness SHALL compute embedding cost locally from token counts and per-model list prices, independent of API-reported usage.

#### Scenario: Per-query and scaled cost reported
- **WHEN** a configuration completes
- **THEN** the result includes cost per query and cost per 1,000,000 queries
- **AND** the cost is derived from `tiktoken` token counts multiplied by the configured per-model price

### Requirement: Structured, reproducible results output

The harness SHALL persist results in a machine-readable form and present a human-readable summary, without depending on the knowledge base.

#### Scenario: Results persisted and summarized
- **WHEN** the benchmark finishes
- **THEN** it writes all per-configuration records to a file under `evals/embeddings/results/`
- **AND** it prints a comparison table summarizing latency, throughput, reliability, and cost across configurations

#### Scenario: No knowledge-base dependency
- **WHEN** the benchmark runs
- **THEN** it uses a built-in synthetic pool of support-style questions as query seeds
- **AND** it requires no ingested knowledge base to run
