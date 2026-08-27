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
