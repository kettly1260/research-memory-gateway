# Real-client benchmark status

Status recorded on 2026-08-28 (Asia/Shanghai).

## Codex

- R01 was executed with the real Codex CLI (`0.150.0-alpha.8`), `gpt-5.6-luna`, low reasoning,
  and the local stdio Agent Surface.
- The agent autonomously called `recall_memory` and returned
  `mem_benchmark_fe_conditions`; the retrieved Fe3+ concentration, HNO3 medium, and HEPES
  concentration were correct and were identified as unverified memory.
- The pilot exposed an incorrect project hint: Codex supplied the repository working directory.
  The gateway now retries globally after a project-filtered zero-hit result, and the tool
  description explicitly says not to use cwd/repository paths as project labels.
- This is one successful pilot result, not the complete 30-case Recall acceptance run.

The follow-up smoke set added:

- R02: autonomous Recall, correct HEPES 20 mM memory, no project fallback required.
- R23: general SQLite FTS5 question answered without a false-positive Recall.
- C63: stable reply-style preference captured as Ambient memory.
- C60: transient pause message correctly ignored without Capture.

The first C63 execution showed that Codex blocks non-read-only MCP calls when its approval policy
is `never`. The successful run used `--approve-for-me`. It also exposed that the tool schema allowed
arbitrary `importance` strings even though the implementation accepts only `auto`, `low`, `normal`,
and `high`; the Agent first sent `medium`, then corrected itself. The Agent Surface now publishes
the four allowed values as a JSON Schema enum.

Research-content Capture cases such as C01 were not sent to the external model after execution
approval rejected the transfer of specific private experiment details. Continuing those cases
requires explicit user authorization for that benchmark data disclosure.

An optimized R02 attempt disabled unrelated Codex plugin/app/browser/computer/image/multi-agent
features and used an ephemeral client. It produced no benchmark row because the client repeatedly
timed out, fell back from WebSocket to HTTPS, then remained unable to reach the model service. The
process was terminated after all transport retries; no Gateway failure was observed.

## ChatGPT Workspace Agent

- The account exposes one blank, unpublished editable Agent:
  `agt_6a901f7b2bf88191b9d7798bf98585a1`.
- The Agent has not been configured or published.
- Research Memory Gateway is not present among the Workspace Agent apps currently available to
  this account, so the custom MCP cannot be attached and a real ChatGPT invocation benchmark
  cannot start.
- Required external step: expose/register the Streamable HTTP MCP as a ChatGPT custom app, attach
  it to a Workspace Agent, and obtain explicit user approval before publishing that Agent.

No simulated ChatGPT or Codex results are recorded as benchmark outcomes.
