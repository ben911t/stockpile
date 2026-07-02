"""Claude Code status line for stockpile.

Two rows, so the usage figures on row 2 are never truncated off the right edge
by a long branch name on row 1 — that one-line truncation was why usage looked
"missing". Row 1: project / branch / model. Row 2: usage — 5-hour & weekly rate
limits, context-window %, and session cost (all from the stdin payload Claude
Code provides; see https://code.claude.com/docs/en/statusline).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

COLOR = "\033[38;2;6;182;212m"   # cyan-500 — project label
RESET = "\033[0m"
DIM = "\033[38;2;220;220;220m"   # light gray — readable on dark terminals
USAGE = "\033[38;2;250;204;21m"  # amber — make the usage row pop
BOLD = "\033[1m"
LABEL = "stockpile"


def main():
    # Claude Code renders the status line as UTF-8; force it so the · / … chars
    # don't get mangled by Windows' default cp1252 stdout.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    cwd = data.get("cwd") or os.getcwd()
    model = (data.get("model") or {}).get("display_name") or "?"
    project_dir = (data.get("workspace") or {}).get("project_dir") or cwd
    output_style = (data.get("output_style") or {}).get("name") or "default"
    thinking_on = (data.get("thinking") or {}).get("enabled", False)
    effort_level = (data.get("effort") or {}).get("level")

    # Usage fields. rate_limits appears only for Pro/Max accounts after the
    # first API response, and each window can be absent; ctx % can be null
    # early in a session — so every piece below is optional.
    ctx_pct = (data.get("context_window") or {}).get("used_percentage")
    rate_limits = data.get("rate_limits") or {}
    five_hr_pct = (rate_limits.get("five_hour") or {}).get("used_percentage")
    seven_d_pct = (rate_limits.get("seven_day") or {}).get("used_percentage")
    cost = (data.get("cost") or {}).get("total_cost_usd")

    try:
        rel = Path(cwd).relative_to(project_dir).as_posix()
        if rel in ("", "."):
            rel = None
    except (ValueError, TypeError):
        rel = cwd

    branch = "?"
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, stderr=subprocess.DEVNULL, text=True, timeout=2,
        )
        branch = out.strip() or "?"
    except Exception:
        pass

    model_str = model
    if thinking_on:
        model_str += " [think]"
    if effort_level:
        model_str += f" [{effort_level}]"

    # Row 1 — identity.
    row1 = [
        f"{BOLD}{COLOR}[{LABEL}]{RESET}",
        f"{DIM}({branch}){RESET}",
        f"{DIM}{model_str}{RESET}",
    ]
    if rel:
        row1.append(f"{DIM}{rel}{RESET}")
    if output_style and output_style != "default":
        row1.append(f"{DIM}<{output_style}>{RESET}")

    # Row 2 — usage, on its own line so a long branch can't push it off-screen.
    usage = []
    if five_hr_pct is not None:
        usage.append(f"5h {five_hr_pct:.0f}%")
    if seven_d_pct is not None:
        usage.append(f"wk {seven_d_pct:.0f}%")
    if ctx_pct is not None:
        usage.append(f"ctx {ctx_pct:.0f}%")
    if cost is not None:
        usage.append(f"${cost:.2f}")
    if usage:
        row2 = f"{USAGE}usage: " + " · ".join(usage) + RESET
    else:
        # No usage yet (e.g. before the first API response, or non-Pro/Max).
        row2 = f"{DIM}usage: awaiting first API response…{RESET}"

    sys.stdout.write(" | ".join(row1) + "\n" + row2)


if __name__ == "__main__":
    main()
