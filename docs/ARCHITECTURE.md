# Architecture

This repository contains two things:

1. **The competition entry** (`submission/`): a declarative Google ADK agent config.
   The organizers compile and run it inside their `swegemma` harness.
2. **A local replica of that harness** (`harness/`, `scripts/`). The official harness
   is not public, and there is only one leaderboard submission per day, so we need
   our own way to measure changes.

Everything in the replica is derived from the organizers' `HARNESS_README.md`,
`sandbox/setup.py`, `docker/Dockerfile.public`, and the competition rules pages.
Where the README is silent, we made a choice and list it below under
"Known divergences".

## Data flow

```
submission/ --validate_submission.py--> dist/submission.zip --> Kaggle (official harness, 4x L4, vLLM)
     |
     +--> harness/compile.py (YAML -> ADK agents, tools bound by name)
                |
harness/run.py: for each task
   Container A (harness/sandbox.py): snapshot + setup.py + "baseline" commit
   ADK Runner <-> Gemini API gemma-4-31b-it   (model proxy)
   tools (harness/tools.py) --docker exec--> Container A
   patch = submit_patch() or fallback `git add -N . && git diff HEAD`
   Container B (harness/verify.py): fresh snapshot + patch + reset test files + test_patch + pytest
   -> results dir: summary.json, task_results.jsonl, patches/, traces/, logs/, test_outputs/
```

## Modules

| Module | Responsibility |
| --- | --- |
| `scripts/validate_submission.py` | Checks a submission against the documented `adk-submission` rules and zips it. Also catches ADK `{state}` placeholders that would raise KeyError at runtime. Run it before every upload. |
| `harness/sandbox.py` | Builds the `swegemma-sandbox:public` image from the organizers' Dockerfile. Runs task containers (`--network none`, 4 GiB, 2 CPUs) and bootstraps them as in README 4.2. |
| `harness/verify.py` | Phase-2 grading (README 8.2): a 4-pass patch apply, a reset of the files the test patch targets, the test patch itself, and the exact pytest command. Grading is per test against a gold reference (see below); the official exit-code verdict is reported as `resolved_exit`. |
| `harness/tools.py` | The 9 agent tools (README 6), with budget gating, truncation, and the 3-tier `edit_file` matcher. `CodeGraph` loads the published graphs and embeddings. |
| `harness/compile.py` | YAML to an ADK agent tree (LlmAgent / Sequential / Parallel / Loop / AgentTool). It never imports submission code. |
| `harness/run.py` | End-to-end runner: the README 5.2 prompt, the README 5.3 outer loop with nudges, fallback patch extraction, grading, and the README 9.2 output layout. It resumes from `task_results.jsonl`. |

## Trust boundaries

- **Agent actions are untrusted.** Model-issued commands run only inside a
  resource-limited container whose network was disconnected (and verified
  offline) at the end of bootstrap, before any agent command or test runs. File tools reject `..` and absolute
  paths outside `/workspace`. Container commands use argv lists, not a local
  shell. In-container commands are killed by coreutils `timeout -s KILL`, because
  killing the local `docker exec` client would leave the process running.
- **Submissions are untrusted input to the compiler.** YAML is parsed with a
  `SafeLoader` subclass whose only extension is a sandboxed `!include`. Paths must
  resolve inside the submission root, and symlinks are rejected. No Python from
  the submission is imported.
- **Test paths from task data** are checked against a conservative character set
  before they are used in the pytest command line.
- **Secrets.** The Gemini API key is read from `GOOGLE_API_KEY` and is never
  written to results or logs.

## Known divergences from the official harness

| Area | Official | Local replica | Impact |
| --- | --- | --- | --- |
| Model | `gemma-4-31b-it-qat-w4a16-ct` on vLLM (gemma4 tool/reasoning parsers) | Gemini API `gemma-4-31b-it` (bf16) | Tool-call formatting, thinking behavior and latency differ. The API rejects `thinking_config`, so it is dropped. |
| Context limit | vLLM `max_model_len=32768`. vLLM rejects any prompt longer than `max_model_len - max_tokens` (`TokenizeParams.max_input_tokens` in `vllm/renderers/params.py`, checked at vLLM main 21ee743) | Enforced by default in `run.py` (stops the session with `context overflow`) | `max_output_tokens` directly shrinks the usable history: 16384 leaves only 16k prompt tokens. `--no-enforce-context` disables the check. |
| Test dependencies | Cached per-repo site-packages, then `setup.py --fast-path` | `setup.py --fast-path`, then the repo's declared runtime and test requirements (`requirements-*.txt`, PEP 735 dependency groups) from PyPI, limited to releases uploaded before the task date + 14 days. The container is online only during this setup, then disconnected and checked offline. | Versions may differ from the official caches. A few tests still cannot pass locally (network timeouts, pygments-version-specific output); per-test grading handles them. |
| Resolution criterion | pytest exit code 0 | Every test that passes with the gold patch must pass (SWE-bench style); references in `RESOURCES/reference_results/` | Tasks with no fail-to-pass test locally are marked `verifiable: false` and excluded from local scores. |
| Patch text | Final newline presumably handled | `tasks.jsonl` stores patches without a final newline; we append it | None (git apply rejects such patches as corrupt otherwise). |
| Instruction section 5 of the prompt | Unpublished wording | Paraphrase of the README summary | Small. |
| Tool descriptions | Unpublished | Our docstrings | The model sees different tool docs than in scoring. |
| Patch apply pass 2 (symlink-normalized) | Present | Omitted | Only matters for symlinked package directories. |
| Skills, LoRA adapters | Supported | Not yet supported locally | Evaluate those only on the leaderboard for now. |
| Timing | vLLM on 4x L4 | About 30 s per call through the free API | Local wall time is not meaningful. Compare tool calls, LLM calls and token counts instead. |

## Facts about the evaluation found so far

- The published code graphs contain only `calls` edges, although the README mentions `DEFINED_IN` and `IMPORTS`.
- The official compaction settings (`token_threshold=32768`, `compaction_interval=15` user turns) mean history is almost never compacted within one task. The agent must be frugal with context itself.
- `hints` is only present in session state when a task has hints (no public task does). An instruction containing `{hints}` would therefore fail every task; use `{hints?}`.

## Operating notes

- Docker access: the user is in the `docker` group. In a shell that predates that, prefix commands with `sg docker -c "..."`.
- Competition data lives outside the repo, in `../RESOURCES/competition_data/`. `scripts/fetch_data.sh` downloads it.
- The Python environment is `.venv/` (gitignored). It needs `google-adk`, `pyyaml`, `numpy` and `pytest`.
