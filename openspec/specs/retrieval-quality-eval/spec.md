# retrieval-quality-eval

## Purpose

Evaluate whether the CHO-17 hybrid RRF retriever (dense vector + FTS + Reciprocal Rank Fusion) surfaces the *right* `kb_faq` chunk for the CHO-18 golden sets, and how well it is ranked. The harness reuses the existing embedding recipe and RRF SQL rather than reimplementing retrieval, computes deterministic ground-truth `chunk_id` metrics (recall, hit@k, MRR) with no LLM, and reference-based DeepEval retriever metrics (ContextualRecall/Precision/Relevancy) scored by Claude. It delivers the payoff the golden sets were built for — quantifying the circularity gap between honest synthetic-query recall and inflated raw-question recall — reports per-topic and quality-filtered coverage, and runs cost-bounded, reproducibly, and read-only against the knowledge base.

## Requirements

### Requirement: Evaluate the hybrid RRF retriever against the golden sets

The harness SHALL load the CHO-18 golden datasets and, for each golden, retrieve from `kb_faq` using the CHO-17 hybrid RRF pipeline (dense vector + FTS + Reciprocal Rank Fusion), reusing the same embedding recipe and SQL rather than reimplementing retrieval.

#### Scenario: Golden query is retrieved through the RRF path
- **WHEN** the harness evaluates a golden
- **THEN** it embeds the golden `input` with the same model and dimensions as `kb_faq.embedding` and runs the RRF query to obtain an ordered top-K list of `kb_faq` ids
- **AND** it reuses the RRF SQL and retrieval config from the existing retrieval benchmark

#### Scenario: Embedding recipe is verified before running
- **WHEN** the harness starts
- **THEN** it asserts the query embedding dimensionality matches `kb_faq.embedding` before scoring any golden

### Requirement: Deterministic ground-truth chunk-id metrics

The harness SHALL compute recall, hit@k, and MRR directly from the ordered retrieved id list against each golden's ground-truth `chunk_id`, without using an LLM.

#### Scenario: Recall and hit@k from the id list
- **WHEN** the top-K retrieved ids are known for a golden
- **THEN** the harness records whether the ground-truth `chunk_id` appears (recall), its rank, hit@k for k in {1,3,5,10}, and MRR (1/rank, or 0 if absent)

#### Scenario: Deterministic metrics need no LLM
- **WHEN** only the chunk-id metrics are requested
- **THEN** the harness produces recall/hit@k/MRR with zero LLM calls

### Requirement: DeepEval reference-based retriever metrics with Claude

The harness SHALL also compute DeepEval's ContextualRecall, ContextualPrecision, and ContextualRelevancy metrics, scored by Claude via the existing `AnthropicModel` wiring, over the retrieved contexts.

#### Scenario: Metrics scored over retrieved contexts
- **WHEN** a golden has been retrieved
- **THEN** the harness builds a DeepEval test case with the golden `input`, `expected_output`, and the retrieved chunk texts as `retrieval_context`, preserving RRF rank order
- **AND** it scores ContextualRecall, ContextualPrecision, and ContextualRelevancy using Claude as the metric model

#### Scenario: Metric wiring verified before the full run
- **WHEN** the harness runs for the first time
- **THEN** it verifies each metric on a single test case before scoring the whole dataset

### Requirement: Synthetic-vs-baseline circularity comparison

The harness SHALL evaluate both the synthetic golden set and the raw-question baseline set through the identical retrieval path and report their recall side by side.

#### Scenario: Circularity gap is reported
- **WHEN** both datasets have been evaluated
- **THEN** the harness reports synthetic recall, baseline recall, and the gap between them (baseline minus synthetic)

### Requirement: Per-topic and quality-filtered reporting

The harness SHALL report retrieval quality broken down by topic and SHALL allow filtering synthetic goldens by their recorded input-quality score.

#### Scenario: Per-topic breakdown
- **WHEN** results are aggregated
- **THEN** recall and hit@k are reported per topic in addition to the overall figures

#### Scenario: Optional quality gate
- **WHEN** a minimum input-quality threshold is provided
- **THEN** synthetic goldens below the threshold are excluded from that run, and the number excluded is reported

### Requirement: Cost-bounded, reproducible, read-only execution

The harness SHALL bound external cost, persist results reproducibly, and never modify the knowledge base.

#### Scenario: Cost controls available
- **WHEN** the harness is invoked
- **THEN** it supports a dry run (few goldens), a golden limit, and selecting a subset of metrics, and caps LLM concurrency to avoid request timeouts

#### Scenario: Results persisted
- **WHEN** evaluation completes
- **THEN** per-golden records and an aggregate plus per-topic summary are written as timestamped files under `evals/quality/results/`

#### Scenario: Read-only against the knowledge base
- **WHEN** the harness runs
- **THEN** it performs only read queries against `kb_faq` and never modifies the table
