# Gemma 4 Developer Agent

Our entry for the Kaggle [Gemma 4 Developer Agent](https://www.kaggle.com/competitions/gemma-4-developer-agent)
competition and its [paper track](https://www.kaggle.com/competitions/gemma-4-developer-agent-paper).

An entry is a declarative Google ADK agent config (YAML, prompts, skills, optional LoRA
adapters). The organizers' `swegemma` harness runs it on ~120 hidden SWE-bench-style Python
tasks, using `gemma-4-31b-it-qat-w4a16-ct` on 4x L4. The score is the fraction of tasks
whose hidden tests pass after our patch is applied.

## Layout

| Path | Purpose |
| --- | --- |
| `submission/` | The agent config that gets zipped and submitted. `agent.yaml` must stay at its root. |
| `scripts/validate_submission.py` | Replica of the documented `adk-submission` checks; `--zip` also packages. |
| `scripts/fetch_data.sh` | Downloads competition data (the snapshot subset listed in `splits/`). |
| `splits/` | Dev task splits. `dev_a` (fastapi) is for tuning; `dev_b` (rich/requests/httpx) is held out to mimic unseen repos. |
| `tests/` | Unit tests for our tooling. |

Competition data is not redistributable. Keep it outside this repository (we use
`../RESOURCES/competition_data/`).

## Usage

```bash
python -m pytest tests -q
python scripts/validate_submission.py submission --zip dist/submission.zip
kaggle competitions submit gemma-4-developer-agent -f dist/submission.zip -m "<what changed>"
```

The limit is one submission per day. Validate before every upload.

## Key constraints (from the competition rules and HARNESS_README.md)

- One base model for every agent: `gemma-4-31b-it-qat-w4a16-ct`. LoRA adapters must be `.safetensors`, rank ≤ 128. Total unpacked size < 3 GiB.
- Only the 9 harness tools, plus sub-agents wrapped as `agent_tool` and skills.
- 32k context. `max_output_tokens` and `thinking_budget` ≤ 32768.
- 12 hours for all tasks, including sandbox setup. Per-task budgets are set in `submission/eval_config.yaml`.
- The patch is `git diff HEAD` of `/workspace`. Scratch files there end up in the patch, and edits to the target test files are discarded.
