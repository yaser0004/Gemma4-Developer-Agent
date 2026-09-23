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
    assert any("only gemma-4-31b" in e for e in validate(make(tmp_path, files)))


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
        ("thinking_config: {thinking_budget: 0}", "thinking_budget"),
        ("thinking_config: {thinking_level: ULTRA}", "thinking_level"),
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
