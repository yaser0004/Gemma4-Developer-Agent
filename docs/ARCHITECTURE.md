# Architecture

> **Update 2026-09-25:** the organizers released the official harness as the Kaggle dataset
> `metric/gemma-4-developer-agent-wheelhouse` (swegemma 0.2.7, adk_submission 0.2.11,
> adk_eval_core 0.1.0, google-adk 1.36.1, vLLM 0.19.1). Prefer it for agent evaluation:
> install it in `.venv-official` (see `scripts/official_compile_check.py`), then run
> `swegemma eval ... --models-yaml <file>` with a models.yaml that maps
> `gemma-4-31b-it-qat-w4a16-ct` to an OpenAI-compatible endpoint (e.g. NVIDIA NIM
> `openai/google/gemma-4-31b-it`), key in `OPENAI_API_KEY`, and `--image` set to the image
> built from `Dockerfile.public`. The replica below remains useful for per-test grading and
> the verifier gate, and as documentation of the harness behavior.

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
- **Skill scripts** (`variants/*/skills/repo-tools/scripts/`) run inside the task sandbox with the
  agent's privileges, and their arguments are model output derived from untrusted issue text. They
  pass arguments to `git` as argv lists (never a shell), cap their own output, and `check.py` is
  read-only (it never touches the tree or the git index).
- **Model tool calls are untrusted input to the scorer's vLLM server.** vLLM 0.19.1's gemma4 tool
  parser (`_parse_gemma4_array`) loops forever when an array argument holds a bare (non-`<|"|>`)
  element that stops at `]`, e.g. `["list[str]"]`. The loop runs on the server's event loop, so every
  later task of the scoring run gets no model response and the run can exceed 12 h (no score). The
  object/dict path always advances. Hence submissions never offer an array-typed argument:
  `validate_submission.py` rejects `get_code_subgraph` and any instruction or skill doc that shows
  list-form `run_skill_script` args (`args=[...]`, `positional_args`); skills take
  `args={"key": "value"}`, which ADK passes as `--key value`. `compare_arms.py` fails a run's safety
  gate on any list-form skill call or any gap over 400 s between steps.

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
| Skills, LoRA adapters | Supported (LoRA reportedly fails to start on the pinned vLLM 0.19.1) | Not supported by the replica | Use the official harness (Kaggle eval kernel) for skills; `tests/test_scout.py` runs the skill scripts the way the skill runner does. Avoid adapters until the vLLM issue is resolved. |
| Timing | vLLM on 4x L4 | About 30 s per call through the free API | Local wall time is not meaningful. Compare tool calls, LLM calls and token counts instead. |

## Facts about the evaluation found so far

- **`thinking_level` breaks every model call in the scorer.** adk_submission maps it to
  `reasoning_effort`, which LiteLLM rejects for the `openai/` route (UnsupportedParamsError).
  Use `thinking_config.include_thoughts` only. The organizers' sample has this problem.
- Tasks are scored sequentially; a run that reaches 12 hours errors (organizer, forum).
- Skill names must be lowercase kebab-case; skill paths resolve from the submission root
  and may not contain `..` (even though `!include` allows in-root `..`).
- The uploaded file must be named `submission.zip`.
- **How skill scripts run** (google-adk 1.36 `SkillToolset`, adk_eval_core `AdkSandboxCodeExecutor`):
  each `run_skill_script` call inlines the whole skill directory into a temporary
  `.adk_exec_<hex>.py` in the workspace, runs it with `python3` from a shell whose cwd is the workspace,
  extracts the skill into a temp dir, `chdir`s there and `runpy`s the script. Consequences:
  - Scripts must find the repository through `$PWD`, not a literal `/workspace`. The Kaggle scorer uses
    the subprocess sandbox, which rewrites `/workspace` only inside command strings, so a hard-coded
    path in a script searches nothing (the v3 `repo-tools` scripts had this bug).
  - The temp file is visible to `git status` while the script runs.
  - Each call counts as one tool call (`check_budget` defaults to counting), and keep the skill
    directory small because it is resent on every call.
- Snapshots carry the repository's git history up to the task's base (the newest commit predates the
  PR), so `git log`/`git blame` are legitimate context and leak nothing.
- **Where task time goes** (traces of arm races 1-3): per LLM call, latency is about
  1.67 s + completion_tokens / 35.8 s at concurrency 1; tool execution is under 5% of task time.
  Thinking text is about 62% of completion tokens and sits in a long tail (p90 about 1,800 chars per
  call). `thinking_budget` is enforced (vLLM `thinking_token_budget` with the gemma4 reasoning parser).
  Compaction (above) fired in 53 of 71 v12 tasks.

- The published code graphs contain only `calls` edges, although the README mentions `DEFINED_IN` and `IMPORTS`.
- Compaction in the scorer uses `token_threshold=14336` (HARNESS_README since 2026-09-25, Getting Started notebook). Compaction summarizes old events, so exact code strings needed later by `edit_file` can be lost; keep tool output short.
- `hints` is only present in session state when a task has hints (no public task does). An instruction containing `{hints}` would therefore fail every task; use `{hints?}`.

## Scorer stack changes (swegemma 0.2.7 -> 0.2.11, adk_submission 0.2.11 -> 0.2.13)

Checked by diffing the installed wheels (`.venv-official` is 0.2.7, `.venv-official2` is 0.2.11/0.2.13):

- **Thinking flags.** `thinking_budget > 0` enables thinking, `0` disables it, and `include_thoughts` only decides whether
  thought parts stay in the history, unless neither budget nor level is set. `include_thoughts: false` alone is therefore
  ambiguous (forum 747914); the validator rejects it. Thinking is on for the strongest public bundles; turning it off
  halved the resolve rate in an independent 129-task ablation (forum 746250).
- **Patch loss.** In 0.2.7 only `TimeoutError` and `LlmCallsLimitExceededError` reached the fallback `git diff`; any other
  exception (for example a context overflow) discarded the work. 0.2.11 catches generic exceptions inside the agent loop
  before the fallback, but a stage that raises still ends the task, so overflow remains a failure to measure.
- **Model registry.** `swegemma.models.registry` is gone; the registry is built from the declared models
  (`swegemma.cli._build_cli_model_registry`). `scripts/official_compile_check.py` handles both layouts.
- **Models.** `gemma-4-12b/e4b/e2b-it-qat-w4a16-ct` are accepted next to the 31B, one model per submission. The 12B needs
  `--hf-overrides` on the vLLM command line (the eval kernel passes it).
- **Environment.** The Kaggle default image moved to Python 3.13, but the wheelhouse is cp312: kernels must pin the
  organizers' image (`docker_image` in `kaggle/build_eval_kernel.py`).
- **ADK 1.36 ParallelAgent.** Compaction can drop a branch's function call while keeping its response (adk-python #7324,
  no 1.x backport). Parallel branches therefore must not call tools (`variants/v9_ppa`).

## Local evaluation on Kaggle

`kaggle/census_kernel` (CPU) verifies gold and empty patches for all public tasks with the Kaggle-side verifier and
writes `eval_main.txt` (tasks where gold resolves and empty fails). `kaggle/build_eval_kernel.py` builds the GPU kernel
(4x L4, vLLM, `Evaluator.run` with concurrency) that runs variants on those tasks with their own `eval_config.yaml`.
`scripts/compare_arms.py` reads the result directories and prints resolved counts, paired wins and losses (overall
and per repo) and the projected scorer wall time; with `--tasks` it adds a failure taxonomy (no patch / gold file
touched but tests fail / wrong file), thought volume, compactions and the first-edit call index.
`--caps` replays each run at shorter time caps and projects scorer hours twice: expected (all tasks) and worst case
(only tasks whose statement is not short, i.e. a hidden set where the prompt's short-issue triage never fires).
A config may go to the leaderboard only if expected <= 11.3 h and worst case <= 12.0 h with `--slowdown 1.37
--setup-verify-s 70` (lb-v6 passed at 12.2 h by this formula, lb-v7 overran at 12.7 h, and an overrun gets no score).
Race kernels may run a variant at a looser cap than it ships with (v16_triage's `eval_config.yaml` says so); the
shipping cap comes from this replay. `short_statement` in `compare_arms.py` is the same rule the v16 prompt states.
`scripts/bench_scout.py` measures the scout skill offline (gold-file recall from the problem statement, no LLM). Daily Kaggle GPU quota resets Saturdays at 00:00 UTC.

## Operating notes

- Docker access: the user is in the `docker` group. In a shell that predates that, prefix commands with `sg docker -c "..."`.
- Competition data lives outside the repo, in `../RESOURCES/competition_data/`. `scripts/fetch_data.sh` downloads it.
- The Python environment is `.venv/` (gitignored). It needs `google-adk`, `pyyaml`, `numpy` and `pytest`.
