from __future__ import annotations

import ast
from pathlib import Path


def test_all_production_mutable_save_calls_bind_caller_state() -> None:
    source_root = Path(__file__).parents[1] / "src" / "groove_serpent"
    missing: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {"save_project", "save_album_project"}:
                continue
            keywords = {item.arg for item in node.keywords}
            if "expected_existing_sha256" not in keywords:
                missing.append(f"{path.name}:{node.lineno}:{node.func.id}")
    assert missing == [], (
        "Every production mutable save must bind the exact caller-observed "
        f"bytes; missing: {', '.join(missing)}"
    )


def test_no_module_bypasses_shared_no_replace_rename() -> None:
    source_root = Path(__file__).parents[1] / "src" / "groove_serpent"
    bypasses: list[str] = []
    for path in sorted(source_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not isinstance(function, ast.Attribute):
                continue
            if not isinstance(function.value, ast.Name):
                continue
            if function.value.id != "os":
                continue
            if function.attr == "link":
                bypasses.append(f"{path.name}:{node.lineno}:os.link")
            if function.attr == "rename" and path.name != "atomic_create.py":
                bypasses.append(f"{path.name}:{node.lineno}:os.rename")
    assert bypasses == [], (
        "Create-only commits must use atomic_create.rename_no_replace; "
        f"bypasses: {', '.join(bypasses)}"
    )
