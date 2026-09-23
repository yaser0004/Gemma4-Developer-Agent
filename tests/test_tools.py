import pytest

from harness.tools import EditError, apply_replacement, resolve_workspace_path

SRC = """def f(x):
    if x:
        return 1
    return 2


def g():
    return 2
"""


def test_exact_match():
    out, n, how = apply_replacement(SRC, "return 1", "return 10")
    assert (n, how) == (1, "exact") and "return 10" in out


def test_ambiguous_exact_match_rejected():
    with pytest.raises(EditError, match="2 locations"):
        apply_replacement(SRC, "return 2", "return 3")


def test_allow_multiple_replaces_all():
    out, n, _ = apply_replacement(SRC, "return 2", "return 3", allow_multiple=True)
    assert n == 2 and out.count("return 3") == 2


def test_flexible_match_reindents_new_string():
    old = "if x:\n    return 1"  # model dropped the function's indentation
    new = "if x is not None:\n    return 1"
    out, _, how = apply_replacement(SRC, old, new)
    assert how == "flexible"
    assert "    if x is not None:\n        return 1\n" in out


def test_regex_match_tolerates_spacing_around_delimiters():
    out, _, how = apply_replacement(SRC, "def f( x ) :", "def f(x, y=None):")
    assert how == "regex" and "def f(x, y=None):" in out


def test_crlf_is_normalized():
    out, _, how = apply_replacement("a = 1\r\nb = 2\r\n", "a = 1\nb = 2", "a = 3\nb = 4")
    assert how == "exact" and out == "a = 3\nb = 4\n"


@pytest.mark.parametrize("old", ["", "not in file at all"])
def test_missing_or_empty_old_string_rejected(old):
    with pytest.raises(EditError):
        apply_replacement(SRC, old, "x")


@pytest.mark.parametrize(
    "given, expected",
    [("src/app.py", "src/app.py"), ("/workspace/src/app.py", "src/app.py"), ("/src/app.py", "src/app.py")],
)
def test_workspace_paths(given, expected):
    assert resolve_workspace_path(given) == expected


@pytest.mark.parametrize("bad", ["../etc/passwd", "src/../../x", "/workspace/../etc", "", "  ", "a\x00b", "/workspace"])
def test_workspace_path_rejections(bad):
    with pytest.raises(ValueError):
        resolve_workspace_path(bad)
