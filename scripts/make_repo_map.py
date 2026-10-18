"""Print a compact map of a Python package: one line per module listing its top-level names.

Used to build the repo-map section of an agent prompt so the agent skips the first locate calls.
Usage: python scripts/make_repo_map.py <repo_root> <package_dir> [max_names_per_file]
"""

import ast
import sys
from pathlib import Path


def top_level_names(path: Path, limit: int) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    names = [n.name for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
    public = [n for n in names if not n.startswith("_")] or names
    return public[:limit]


def main() -> None:
    root, package = Path(sys.argv[1]), sys.argv[2]
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    for path in sorted((root / package).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            names = top_level_names(path, limit)
        except SyntaxError:
            continue
        print(f"{rel}: {', '.join(names)}" if names else rel)


if __name__ == "__main__":
    main()
