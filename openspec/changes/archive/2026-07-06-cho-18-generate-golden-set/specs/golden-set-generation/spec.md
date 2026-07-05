## ADDED Requirements

### Requirement: Synthesize goldens from kb_faq chunks

The harness SHALL generate synthetic goldens from `kb_faq` chunks using DeepEval's `generate_goldens_from_contexts`, treating each chunk as its own context, and SHALL NOT re-chunk or reuse the document-loading path.

#### Scenario: Chunks are used as contexts
- **WHEN** the harness generates the synthetic golden set
- **THEN** it pulls `chunk` values from `kb_faq` and passes each chunk as a single-item context
- **AND** it uses `generate_goldens_from_contexts` (not `generate_goldens_from_docs`)

#### Scenario: One golden per context
- **WHEN** goldens are generated
- **THEN** the harness requests at most one golden per context (`max_goldens_per_context = 1`)

### Requirement: Queries are paraphrases, not copies

The synthetic queries SHALL be paraphrased so retrieval is not trivially circular, since the source chunk contains the verbatim FAQ question.

#### Scenario: Evolutions disabled
- **WHEN** the synthetic goldens are generated
- **THEN** evolutions are disabled so queries remain answerable from their single source chunk

#### Scenario: Query does not echo the verbatim question
- **WHEN** a generated golden is inspected
- **THEN** its `input` is a reformulation of the topic, not a verbatim copy of the chunk's `Q:` line

### Requirement: Ground-truth chunk id retained

Each golden SHALL retain the id of the `kb_faq` chunk it was generated from, so downstream retrieval evaluation can check whether that chunk was retrieved.

#### Scenario: Golden carries its source chunk id
- **WHEN** a golden is produced from context index `i`
- **THEN** the harness attaches `chunk_id = id_map[i]`, the `kb_faq.id` of the chunk at that index
- **AND** every saved golden has a non-null `chunk_id` that exists in `kb_faq`

### Requirement: Topic-stratified sampling

The harness SHALL sample chunks stratified across topics so all knowledge-base areas are represented.

#### Scenario: Sample spans all topics
- **WHEN** the harness selects chunks to synthesize from
- **THEN** it samples approximately 250 distinct chunks stratified across the 18 topics
- **AND** the topic distribution of the sample is roughly proportional to the table

### Requirement: Raw-question baseline set

The harness SHALL also produce a small baseline golden set built from real `kb_faq.question` values, saved separately from the synthetic set.

#### Scenario: Baseline built from real questions
- **WHEN** the baseline set is generated
- **THEN** it uses real `kb_faq.question` as `input`, the matching `answer` as `expected_output`, and the row `id` as `chunk_id`
- **AND** it is saved as a separate dataset from the synthetic goldens

### Requirement: Claude as the generation and scoring model

The harness SHALL use Claude for DeepEval generation and quality scoring, wired through DeepEval's model interface.

#### Scenario: Claude wired into DeepEval
- **WHEN** the Synthesizer is constructed
- **THEN** its model is Claude (`claude-sonnet-5`) via a `DeepEvalBaseLLM` wrapper over the Anthropic SDK, or the installed version's native Anthropic model class
- **AND** the wrapper is verified with a single generation call before bulk generation runs

### Requirement: Reproducible dataset output

The harness SHALL persist both golden sets in a machine-readable form, read-only against the knowledge base.

#### Scenario: Datasets saved with required fields
- **WHEN** generation completes
- **THEN** the synthetic and baseline datasets are written to `evals/quality/goldens/` as timestamped JSON
- **AND** each golden includes `input`, `expected_output`, `context`, and `chunk_id`

#### Scenario: Read-only against the knowledge base
- **WHEN** the harness runs
- **THEN** it performs only read queries against `kb_faq` and never modifies the table
