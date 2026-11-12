# Gemma 4 Developer Agent

An autonomous coding agent that fixes real bugs in open-source Python projects, built on
Google's open-weight **Gemma 4** model for the Kaggle
[Gemma 4 Developer Agent](https://www.kaggle.com/competitions/gemma-4-developer-agent) competition.

## What it does

The agent gets a GitHub issue and the repository it belongs to. It has to find the cause,
edit the code, and produce a patch. Then hidden tests decide whether the fix is right. It is
scored on about 120 unseen SWE-bench-style tasks, and the score is the share it solves.

## Why it's hard

- **Small, fixed model.** Every entry runs the same quantized 31B Gemma 4 model on 4x NVIDIA L4 GPUs.
  You can't scale up the model, so all the gains have to come from how the agent works.
- **Strict budgets.** It has a 32k-token context, nine sandboxed tools, and 12 hours for the whole
  run. If the run goes over, the submission gets no score at all.
- **Few chances to measure.** The leaderboard allows one submission a day, and the scores are noisy.

## Approach

An entry is a declarative agent config (YAML prompts, sub-agents, and skills for Google ADK), not code.
Most of the work is the infrastructure needed to improve that config with evidence instead of guesses:

- **A local replica of the official harness.** It has a Docker sandbox, the nine tools, a YAML-to-agent
  compiler, and a SWE-bench-style verifier. I built it before the organizers released theirs.
- **A private Kaggle GPU evaluation kernel.** It runs agent variants head to head on the same tasks, with
  the scorer's exact model server.
- **"Arm race" analysis.** It compares variants with paired wins and losses, a failure taxonomy, and a
  wall-time projection that blocks any config likely to break the 12-hour limit.
- **Safety findings in the scorer stack.** One example is a vLLM tool-parser hang that could stall a whole
  scoring run. The submission validator enforces these findings.

More than 20 agent variants have been raced so far. The [`variants/`](variants/) directory has the full history.

## Results so far

| Submission | Public leaderboard |
| --- | --- |
| First baseline (v2) | 0.03 |
| Anchor config (v6) | 0.13 |
| Time-budgeted config (v12) | **0.15** (reproduced twice) |

The best score went from 0.03 to 0.15 (5x). The leaders score about 0.24, and most of them run forks of one
public config. A paper on the method is planned for the competition's
[paper track](https://www.kaggle.com/competitions/gemma-4-developer-agent-paper).

## Learn more

- [Development guide](docs/DEVELOPMENT.md): repository layout, setup, testing, and how to submit
- [Architecture](docs/ARCHITECTURE.md): data flow, trust boundaries, and how the replica differs from the official harness
