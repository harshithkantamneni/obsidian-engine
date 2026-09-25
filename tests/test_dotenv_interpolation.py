"""Every .env load takes values literally: no ${VAR} expansion anywhere."""
import ast
import os
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# hidden dirs (.git, .venv, .claude worktrees...) are skipped too
_SKIP_DIRS = {"node_modules", "remotion", "dashboard", "outputs", "tests",
              "__pycache__", "site-packages"}
_NEEDS_FLAG = {"load_dotenv", "dotenv_values", "DotEnv"}  # interpolate defaults to True
_FORBIDDEN = {"get_key"}                                  # always interpolates
_EXPECTED_LOADERS = 13                                    # production call sites


def _python_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
                       and not (Path(dirpath) / d / "pyvenv.cfg").exists()]
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


_SUBMODULES = {"main", "parser", "variables", "cli", "ipython", "version"}


def _dotenv_names(tree):
    """Local names bound to python-dotenv functions and (sub)modules in this file."""
    funcs, modules = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "dotenv":
            for a in node.names:
                if a.name in _SUBMODULES:
                    modules.add(a.asname or a.name)
                else:
                    funcs[a.asname or a.name] = a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "dotenv":
                    modules.add(a.asname or a.name.split(".")[0])
    return funcs, modules


def _base_name(node):
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _dotenv_uses(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    funcs, modules = _dotenv_names(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in funcs:
                yield funcs[f.id], node
            elif isinstance(f, ast.Attribute) and _base_name(f.value) in modules:
                yield f.attr, node


def test_every_dotenv_load_disables_interpolation():
    offenders, loaders = [], 0
    for path in _python_files():
        for name, call in _dotenv_uses(path):
            where = f"{path.relative_to(ROOT)}:{call.lineno}"
            if name in _FORBIDDEN:
                offenders.append(f"{where} uses {name}")
            elif name in _NEEDS_FLAG:
                loaders += 1
                kw = {k.arg: k.value for k in call.keywords}
                flag = kw.get("interpolate")
                if not (isinstance(flag, ast.Constant) and flag.value is False):
                    offenders.append(f"{where} {name} without interpolate=False")
    assert offenders == []
    assert loaders == _EXPECTED_LOADERS, (
        f"found {loaders} .env loaders, expected {_EXPECTED_LOADERS}; "
        "update _EXPECTED_LOADERS if one was added or removed on purpose")


def test_load_dotenv_without_interpolation_keeps_references(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=one\nB=${A}\n")
    with patch.dict(os.environ, {}, clear=True):
        load_dotenv(env, interpolate=False)
        assert os.environ["B"] == "${A}"
