import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_submission import main, validate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MODEL = "gemma-4-31b-it-qat-w4a16-ct"


def make(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    return tmp_path


def minimal(**extra: str | bytes) -> dict[str, str | bytes]:
    return {
        "agent.yaml": f"name: a\nmodel: {MODEL}\ninstruction: !include prompts/s.md\ntools: [run_command, submit_patch]\n",
        "prompts/s.md": "hi",
        **extra,
    }


def test_real_submission_is_valid():
    assert validate(REPO / "submission") == []


def test_minimal_valid(tmp_path):
    assert validate(make(tmp_path, minimal())) == []


def test_missing_root(tmp_path):
    errors = validate(make(tmp_path, {"prompts/s.md": "hi"}))
    assert any("missing root config" in e for e in errors)


def test_multiple_roots(tmp_path):
    errors = validate(make(tmp_path, minimal(**{"root_agent.yaml": f"name: b\nmodel: {MODEL}\n"})))
    assert any("multiple root configs" in e for e in errors)


def test_include_traversal_rejected(tmp_path):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ninstruction: !include ../secret.md\n"
    (tmp_path.parent / "secret.md").write_text("x")
    errors = validate(make(tmp_path, files))
    assert any("traversal" in e for e in errors)


def test_include_parent_within_root_allowed(tmp_path):
    files = minimal(**{"sub/a.yaml": f"name: s\nmodel: {MODEL}\ninstruction: !include ../prompts/s.md\n"})
    files["agent.yaml"] += "sub_agents:\n  - config_path: sub/a.yaml\n"
    assert validate(make(tmp_path, files)) == []


def test_official_sample_submission_only_fails_on_scorer_hazards():
    # The sample is structurally valid, but its thinking_level makes every model call
    # fail in the scorer (reasoning_effort rejected by LiteLLM), and it offers the
    # array-argument graph tool that can hang the scorer's tool parser.
    sample = REPO.parent / "RESOURCES" / "competition_data" / "sample_submission"
    if not sample.is_dir():
        pytest.skip("competition data not downloaded")
    errors = validate(sample)
    assert errors and all("reasoning_effort" in e or "array argument" in e for e in errors)


def test_include_absolute_rejected(tmp_path):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ninstruction: !include /etc/passwd\n"
    assert any("absolute" in e for e in validate(make(tmp_path, files)))


def test_include_cycle_rejected(tmp_path):
    files = minimal(**{"c/a.yaml": "x: !include b.yaml\n", "c/b.yaml": "y: !include a.yaml\n"})
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ngenerate_content_config: !include c/a.yaml\n"
    assert any("cycle" in e for e in validate(make(tmp_path, files)))


def test_symlink_rejected(tmp_path):
    root = make(tmp_path, minimal())
    (root / "prompts" / "link.md").symlink_to(root / "prompts" / "s.md")
    assert any("symlink" in e for e in validate(root))


def test_pickle_weights_rejected(tmp_path):
    errors = validate(make(tmp_path, minimal(**{"adapters/x/adapter_model.bin": b"\x80"})))
    assert any("extension" in e for e in errors)


def test_two_models_rejected(tmp_path):
    files = minimal(**{"sub.yaml": "name: s\nmodel: gemma-4-9b-it\n"})
    files["agent.yaml"] = f"name: a\nmodel: openai/{MODEL}\nsub_agents:\n  - config_path: sub.yaml\n"
    assert any("single-model" in e for e in validate(make(tmp_path, files)))


def test_other_single_model_rejected(tmp_path):
    files = minimal()
    files["agent.yaml"] = "name: a\nmodel: gemma-4-9b-it\n"
    assert any("are supported" in e for e in validate(make(tmp_path, files)))


def test_agent_tool_model_checked(tmp_path):
    files = minimal(**{"t.yaml": "name: t\nmodel: gemma-4-e2b-it\n"})
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ntools:\n  - agent_tool:\n      config_path: t.yaml\n"
    assert any("single-model" in e for e in validate(make(tmp_path, files)))


def test_unknown_tool_rejected(tmp_path):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ntools: [google_search]\n"
    assert any("not a harness tool" in e for e in validate(make(tmp_path, files)))


@pytest.mark.parametrize(
    "gen, needle",
    [
        ("max_output_tokens: 40000", "max_output_tokens"),
        ("thinking_config: {thinking_budget: -1}", "thinking_budget"),
        ("thinking_config: {include_thoughts: false}", "ambiguous"),
        ("thinking_config: {thinking_level: ULTRA}", "thinking_level"),
        ("thinking_config: {thinking_level: high}", "reasoning_effort"),
        ("top_p: 1.5", "top_p"),
        ("system_instruction: hi", "not allowed"),
    ],
)
def test_generation_limits(tmp_path, gen, needle):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ngenerate_content_config:\n  {gen}\n"
    assert any(needle in e for e in validate(make(tmp_path, files)))


def test_missing_adapter_rejected(tmp_path):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\nadapter: main\n"
    assert any("adapter 'main'" in e for e in validate(make(tmp_path, files)))


def test_skill_without_frontmatter_rejected(tmp_path):
    files = minimal(**{"skills/s/SKILL.md": "no frontmatter"})
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\nskills: [skills/s]\n"
    assert any("frontmatter" in e for e in validate(make(tmp_path, files)))


def test_zip_has_root_agent_yaml(tmp_path):
    src = make(tmp_path / "sub", minimal())
    out = tmp_path / "out.zip"
    assert main([str(src), "--zip", str(out)]) == 0
    assert "agent.yaml" in zipfile.ZipFile(out).namelist()


def test_zip_not_written_when_invalid(tmp_path):
    src = make(tmp_path / "sub", {"prompts/s.md": "hi"})
    out = tmp_path / "out.zip"
    assert main([str(src), "--zip", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize(
    "prompt, ok",
    [
        ("Fix: {problem_description}", True),
        ("Hints: {hints?}", True),
        ('JSON like {"a": 1} is left alone', True),
        ("Hints: {hints}", False),
        ("Edit {filename} now", False),
        ("Escaped? {{filename}}", False),
        ("Uses {analysis}", True),  # written by the sub-agent's output_key below
    ],
)
def test_instruction_placeholders(tmp_path, prompt, ok):
    files = minimal(**{"prompts/s.md": prompt, "sub.yaml": f"name: s\nmodel: {MODEL}\noutput_key: analysis\n"})
    files["agent.yaml"] += "sub_agents:\n  - config_path: sub.yaml\n"
    errors = validate(make(tmp_path, files))
    assert (not any("placeholder" in e for e in errors)) == ok, errors


def test_skill_paths_resolve_from_root_and_reject_parent(tmp_path):
    skill = "---\nname: s\n---\nbody\n"
    files = minimal(**{"skills/s/SKILL.md": skill, "sub/a.yaml": f"name: t\nmodel: {MODEL}\nskills: [skills/s]\n"})
    files["agent.yaml"] += "sub_agents:\n  - config_path: sub/a.yaml\n"
    assert validate(make(tmp_path / "ok", files)) == []
    files["sub/a.yaml"] = f"name: t\nmodel: {MODEL}\nskills: [../skills/s]\n"
    assert any("must not contain '..'" in e for e in validate(make(tmp_path / "bad", files)))


def test_skill_name_must_be_kebab_case(tmp_path):
    files = minimal(**{"skills/s/SKILL.md": "---\nname: my_skill\n---\nbody\n"})
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\nskills: [skills/s]\n"
    assert any("kebab-case" in e for e in validate(make(tmp_path, files)))


@pytest.mark.parametrize("alias", ["gemma-4-12b-it-qat-w4a16-ct", "gemma-4-e4b-it-qat-w4a16-ct", "gemma-4-e2b-it-qat-w4a16-ct"])
def test_smaller_qat_variants_accepted(tmp_path, alias):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {alias}\n"
    assert validate(make(tmp_path, files)) == []


def test_mixing_qat_variants_rejected(tmp_path):
    files = minimal(**{"sub.yaml": "name: s\nmodel: gemma-4-12b-it-qat-w4a16-ct\n"})
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\nsub_agents:\n  - config_path: sub.yaml\n"
    assert any("single-model" in e for e in validate(make(tmp_path, files)))


def test_explicit_thinking_off_and_on_accepted(tmp_path):
    for gen in ("thinking_config: {thinking_budget: 0, include_thoughts: false}",
                "thinking_config: {thinking_budget: 2048, include_thoughts: false}"):
        files = minimal()
        files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ngenerate_content_config:\n  {gen}\n"
        assert validate(make(tmp_path / str(abs(hash(gen))), files)) == []


def test_array_argument_tool_rejected(tmp_path):
    files = minimal()
    files["agent.yaml"] = f"name: a\nmodel: {MODEL}\ntools: [run_command, get_code_subgraph]\n"
    assert any("array argument" in e for e in validate(make(tmp_path, files)))


@pytest.mark.parametrize(
    "doc, ok",
    [
        ('Run `run_skill_script(skill_name="t", file_path="scripts/s.py", args={"terms": "a b"})`.', True),
        ('Run `run_skill_script(skill_name="t", file_path="scripts/s.py", args=["a", "b"])`.', False),
        ("Pass the terms as positional_args.", False),
    ],
)
def test_list_form_skill_args_rejected_in_skill_docs(tmp_path, doc, ok):
    files = minimal(**{"skills/t/SKILL.md": f"---\nname: t\n---\n{doc}\n"})
    files["agent.yaml"] += "skills: [skills/t]\n"
    errors = validate(make(tmp_path, files))
    assert (not any("list-form" in e for e in errors)) == ok, errors


def test_list_form_skill_args_rejected_in_prompt(tmp_path):
    files = minimal(**{"prompts/s.md": "Call run_skill_script with args: [the terms]."})
    assert any("list-form" in e for e in validate(make(tmp_path, files)))


def test_missing_time_cap_warns_but_passes(tmp_path, capsys):
    assert main([str(make(tmp_path, minimal()))]) == 0
    assert "max_time_minutes" in capsys.readouterr().err
    files = minimal(**{"eval_config.yaml": "evaluation:\n  max_time_minutes: 6\n"})
    assert main([str(make(tmp_path / "capped", files))]) == 0
    assert "max_time_minutes" not in capsys.readouterr().err
