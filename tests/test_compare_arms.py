import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare_arms import patched_files, trace_stats  # noqa: E402


def step(prompt_tokens, *tools):
    calls = [{"function_name": t, "arguments": {}} for t in tools]
    return {"metrics": repr({"prompt_tokens": prompt_tokens, "completion_tokens": 10}),
            "tool_calls": repr(calls)}


def test_trace_stats_counts_prompt_drops_and_first_edit():
    trace = {"steps": [
        {"source": "system"},
        step(4000, "run_command"),
        step(9000, "read_file"),
        step(14000, "run_command"),
        step(6000, "edit_file"),   # compaction: >20% drop
        step(5500, "run_command"),  # small drop is not a compaction
    ]}
    assert trace_stats(trace) == (1, 4)


def test_trace_stats_without_edits_or_metrics():
    assert trace_stats({"steps": [{"tool_calls": "not a literal"}, {}]}) == (0, None)
    assert trace_stats({}) == (0, None)


def test_patched_files_reads_both_diff_styles():
    gold = "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1 +1 @@\n-a\n+b\n"
    agent = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
    assert patched_files(gold) == {"pkg/mod.py"}
    assert patched_files(agent) == {"x.py"}
    assert patched_files("") == set()
