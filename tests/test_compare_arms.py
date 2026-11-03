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


def test_short_statement_ignores_links_comments_and_fixes_lines():
    from compare_arms import short_statement
    assert short_statement("Syntax padding\n\nFixes https://github.com/o/r/issues/3727")
    assert short_statement("empty with end\n\n- Fixes https://x/1\n- Closes #2\n<!-- template\nlong text -->")
    assert not short_statement("Calling `Table.add_row` with a `None` cell raises TypeError: "
                               "'NoneType' object is not iterable; it should render an empty cell.")


def test_safety_stats_counts_rejections_retries_list_args_and_stalls():
    from compare_arms import safety_stats
    skill = {"function_name": "run_skill_script", "arguments": {"skill_name": "t", "args": ["list[str]"]}}
    edit = {"function_name": "edit_file", "arguments": {"filepath": "a.py"}}
    trace = {"steps": [
        {"tool_calls": repr([edit]), "extra": repr({"elapsed_s": 10.0}),
         "observation": repr({"content": "mandatory input parameters are not present: old_string"})},
        {"tool_calls": repr([edit]), "extra": repr({"elapsed_s": 20.0})},
        {"tool_calls": repr([skill]), "extra": repr({"elapsed_s": 500.0})},
        {"tool_calls": repr([{"function_name": "run_skill_script", "arguments": {"args": {"terms": "a"}}}]),
         "extra": repr({"elapsed_s": 510.0})},
    ]}
    stats = safety_stats(trace)
    assert stats["rejected"] == 1
    assert stats["identical_retries"] == 1
    assert stats["list_skill_args"] == 1
    assert stats["stalls"] == 1


def test_project_replays_a_shorter_cap():
    from compare_arms import project
    rows = {"a": {"resolved": True, "duration_seconds": 100.0},
            "b": {"resolved": True, "duration_seconds": 500.0}}
    resolved, hours = project(rows, cap=300, slowdown=1.0, setup_verify_s=0, n_scorer=36)
    assert resolved == 1
    assert abs(hours - 36 * 200 / 3600) < 1e-9
    assert project({}, 300, 1.0, 0, 120) == (0, 0.0)


def test_export_writes_metrics_without_patches(tmp_path):
    import csv
    import json
    from compare_arms import EXPORT_FIELDS, export
    run = tmp_path / "arm"
    (run / "traces").mkdir(parents=True)
    (run / "traces" / "trace_t1.json").write_text(json.dumps({"steps": [
        {"observation": "mandatory input parameters are not present"}]}))
    rows = {"t1": {"repo": "r", "resolved": True, "duration_seconds": 12.5, "tool_calls": 3,
                   "agent_patch": "diff --git a/secret.py"},
            "t2": {"repo": "r", "resolved": False, "duration_seconds": 40.0, "tool_calls": 9}}
    out = tmp_path / "m.csv"
    export(out, [run], {"arm": rows}, short_ids={"t2"})
    got = list(csv.DictReader(out.open()))
    assert list(got[0]) == EXPORT_FIELDS
    assert [(r["instance_id"], r["resolved"], r["short_statement"], r["rejected"]) for r in got] == [
        ("t1", "1", "0", "1"), ("t2", "0", "1", "0")]
    assert "diff --git" not in out.read_text()
