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
| `harness/` | Local replica of the official harness: Docker sandbox, the 9 tools, YAML-to-ADK compiler, agent runner, and Phase-2 verifier. See `docs/ARCHITECTURE.md`. |
| `kaggle/build_eval_kernel.py` | Builds a private Kaggle kernel (NvidiaL4, internet off, organizers' wheelhouse) that serves the QAT model with the scorer's vLLM and runs the official Evaluator on chosen variants and tasks. Uses Kaggle GPU quota; never run it while a leaderboard submission is scoring. T4 machines cannot run Gemma 4 attention in vLLM 0.19.1. |
| `docs/ARCHITECTURE.md` | Data flow, trust boundaries, and every known divergence from the official harness. |
| `tests/` | Unit tests for our tooling (Docker tests are skipped when Docker is unavailable). |

Competition data is not redistributable. Keep it outside this repository (we use
`../RESOURCES/competition_data/`).

## Usage

```bash
python -m pytest tests -q
python scripts/validate_submission.py submission --zip dist/submission.zip
kaggle competitions submit gemma-4-developer-agent -f dist/submission.zip -m "<what changed>"   # the uploaded file must be named submission.zip
```

The limit is one submission per day. Validate before every upload.

Local evaluation (needs Docker, the downloaded data, and a Gemini API key):

```bash
python -m venv .venv && .venv/bin/pip install google-adk==2.9.2 pyyaml numpy pytest
.venv/bin/python -c "from harness.sandbox import build_image; build_image()"   # once
.venv/bin/python -m harness.verify --split splits/dev_b.txt --patches gold --results-dir <out>   # verifier self-check
GOOGLE_API_KEY=... .venv/bin/python -m harness.run --submission submission --split splits/dev_b.txt --results-dir <out>
```

## Key constraints (from the competition rules and HARNESS_README.md)

- One base model for every agent: `gemma-4-31b-it-qat-w4a16-ct`. LoRA adapters must be `.safetensors`, rank ≤ 128. Total unpacked size < 3 GiB.
- Only the 9 harness tools, plus sub-agents wrapped as `agent_tool` and skills.
- 32k context. `max_output_tokens` and `thinking_budget` ≤ 32768.
- 12 hours for all tasks, including sandbox setup. Per-task budgets are set in `submission/eval_config.yaml`.
- The patch is `git diff HEAD` of `/workspace`. Scratch files there end up in the patch, and edits to the target test files are discarded.
