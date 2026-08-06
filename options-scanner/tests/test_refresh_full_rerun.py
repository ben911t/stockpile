"""The 🔄 refresh buttons must trigger a FULL rerun, not a fragment rerun.

The Trades list and the Positions table each render inside an ``@st.fragment``,
so a button click there reruns only that fragment. But the top-bar "Schwab (6D)"
token countdown and run_app's token-mtime cache invalidation are computed at
script level — with a fragment-only rerun, hitting 🔄 right after
re-authenticating re-fetched the positions while the toggle kept reading
"Schwab (expired)" until you switched tabs.

Guard: every 🔄 handler ends in a plain ``st.rerun()``.
"""

import ast
from pathlib import Path

import pytest

_TABS = Path(__file__).resolve().parents[1] / "options_scanner" / "tabs"

REFRESH_BUTTONS = [
    ("trades.py", "trades_refresh"),
    ("trades.py", "opt_pos_refresh"),
]


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def _attr_path(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_attr_path(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _refresh_block(module: ast.Module, key: str) -> ast.If:
    for node in ast.walk(module):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and _attr_path(node.test.func) == "st.button"
                and _kwarg(node.test, "key") == key):
            return node
    raise AssertionError(f"no `if st.button(..., key={key!r})` block found")


@pytest.mark.parametrize("filename,key", REFRESH_BUTTONS)
def test_refresh_button_forces_full_rerun(filename, key):
    module = ast.parse((_TABS / filename).read_text(encoding="utf-8"))
    reruns = [
        n for n in ast.walk(_refresh_block(module, key))
        if isinstance(n, ast.Call) and _attr_path(n.func) == "st.rerun"
    ]
    assert reruns, (
        f"{filename}: the {key} handler must call st.rerun() so the top-bar "
        "Schwab token countdown refreshes with the data")
    for call in reruns:
        assert _kwarg(call, "scope") != "fragment", (
            f"{filename}: {key} must rerun the whole app, not the fragment")
