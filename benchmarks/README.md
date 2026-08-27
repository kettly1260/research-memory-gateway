# Research Memory Gateway V2 Agent Invocation Benchmark

This benchmark measures whether an agent **chooses** the V2 memory tools in natural conversations. It is intentionally separate from unit tests that only prove the tools execute correctly.

## Datasets

- `recall_cases.jsonl`: 30 natural prompts covering explicit history, continuation, implicit history, and negative samples.
- `capture_cases.jsonl`: 71 durable/non-durable outputs. The adversarial/generalization set spans polymers, ceramics, electrochemistry, spectroscopy, thermal analysis, mechanical/electrical properties, surface/microstructure characterization, literature conclusions, causal/association research hypotheses, explicit stable preferences, software state, sensitive configuration, speculation, and ordinary chat.

Each client should be tested with the same Agent Surface and the concise V2 skill/system prompt. Do not prepend instructions such as “call recall_memory”.

## Result records

Create one JSONL record per benchmark case.

Recall result example:

```json
{"case_id":"R01","did_recall":true,"correct_memory":true,"false_positive":false,"latency_ms":412,"token_usage":880}
```

Capture result example:

```json
{"case_id":"C01","did_capture":true,"actual_tier":"trusted","duplicate":false,"false_capture":false,"proposal_correct":true}
```

## Scoring

```powershell
python benchmarks/score_invocations.py recall benchmarks/recall_cases.jsonl results/chatgpt-recall.jsonl
python benchmarks/score_invocations.py capture benchmarks/capture_cases.jsonl results/chatgpt-capture.jsonl
```

Add `--require-pass` in CI or release acceptance. The command then exits non-zero when the
result set is incomplete or a target gate fails. The scorer rejects duplicate and unknown
case IDs, reports missing cases, and summarizes latency/token samples when supplied.

Target gates from the V2 plan:

- Explicit historical recall rate: >= 90%.
- Top-5 correct memory rate: >= 90%.
- Recall false-positive rate on negative samples: < 10%.
- Durable capture rate: >= 80%.
- Capture false-positive rate on non-durable chat: < 10%.

Invocation benchmarks require a real client/model run. CI can validate dataset shape and score recorded runs, but it cannot honestly claim ChatGPT/Codex invocation rates without executing those clients.

The direct Gateway classifier test is intentionally weaker than the invocation benchmark: it checks whether `capture_memory` classifies these cases correctly **after the tool has already been called**. It does not prove that an agent will autonomously decide to call `capture_memory`, and it should not be used as a substitute for real client result JSONL files.
