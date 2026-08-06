"""Load options-scanner configuration.

This module prefers values found in a local `config.toml`. Environment
variables may be used to override values from the file or to provide Schwab
credentials when the TOML file is absent. Environment variables are
expected to be of the form `STOCKPILE_*` (examples below).

Recognized environment variables:
  STOCKPILE_SCHWAB_APP_KEY
  STOCKPILE_SCHWAB_APP_SECRET
  STOCKPILE_SCHWAB_CALLBACK_URL
  STOCKPILE_SCHWAB_TOKEN_FILE
"""

import os
import re
import tomllib
from pathlib import Path

_CONFIG_PATH = Path(__file__).parents[1] / "config.toml"


_ERR_LINE_RE = re.compile(r"line (\d+)")


def _error_lineno(err: tomllib.TOMLDecodeError) -> int | None:
    """1-based line of a TOML parse error. Python 3.14 exposes ``.lineno``;
    older versions only carry it in the message text."""
    lineno = getattr(err, "lineno", None)
    if isinstance(lineno, int):
        return lineno
    m = _ERR_LINE_RE.search(str(err))
    return int(m.group(1)) if m else None


def _describe_dropped_line(text: str, lineno: int) -> str:
    """Human-readable note for a config line we had to ignore to keep the rest
    of the file parseable."""
    key = text.split("=", 1)[0].strip() if "=" in text else ""
    if key == "paper":
        val = text.split("=", 1)[1].strip()
        return (
            f"config.toml line {lineno}: paper = {val} is not valid — paper "
            f"must be the boolean true or false (not yes/no or a quoted "
            f"string). Defaulting to paper mode; no live orders will be placed."
        )
    return f"config.toml line {lineno} ignored (invalid TOML): {text.strip()!r}"


def _load_toml_lenient(path: Path) -> tuple[dict, list[str]]:
    """Parse ``path`` as TOML. If it doesn't parse, drop the offending line(s)
    one at a time and retry, so a single bad value (e.g. ``paper = yes``)
    doesn't discard valid config like Schwab credentials — the token countdown
    and everything else keep working. Returns ``(cfg, warnings)``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return {}, [f"config.toml could not be read ({e})."]

    try:
        return tomllib.loads(raw), []
    except tomllib.TOMLDecodeError:
        pass  # fall through to line-by-line recovery

    lines = raw.splitlines()
    warnings: list[str] = []
    # Cap retries at the line count so a pathological file can't loop forever.
    for _ in range(len(lines) + 1):
        try:
            return tomllib.loads("\n".join(lines)), warnings
        except tomllib.TOMLDecodeError as e:
            lineno = _error_lineno(e)
            if (lineno is None or not (1 <= lineno <= len(lines))
                    or not lines[lineno - 1].strip()):
                # Can't localize the error — give up and use safe defaults.
                warnings.append(
                    f"config.toml could not be parsed ({e}). Falling back to "
                    f"built-in defaults (Schwab in paper mode)."
                )
                return {}, warnings
            warnings.append(_describe_dropped_line(lines[lineno - 1], lineno))
            lines[lineno - 1] = ""  # blank it, preserving later line numbers
    return {}, warnings


def _normalize_paper(schwab: dict, warnings: list[str]) -> None:
    """Coerce ``schwab['paper']`` to a real bool. Anything that isn't already
    a TOML boolean warns and falls back to True (paper mode), so placing live
    orders always requires an explicit, unambiguous ``paper = false``."""
    if "paper" not in schwab or isinstance(schwab["paper"], bool):
        return
    bad = schwab["paper"]
    schwab["paper"] = True
    warnings.append(
        f"config.toml: [schwab] paper = {bad!r} is not valid — paper must be "
        f"the boolean true or false. Defaulting to paper mode (no live orders "
        f"will be placed)."
    )


def load_config() -> dict:
    """Return config dict.

    If `config.toml` exists it is loaded first. Any recognized environment
    variables will then be applied on top of the TOML-derived values so
    environment values take precedence. If the TOML file is missing Schwab
    credentials from environment variables will still be applied.

    A malformed config never crashes the caller: an invalid line (e.g.
    ``paper = yes``) is dropped so the rest of the file still loads, and a
    non-boolean ``paper`` flag falls back to paper mode. Any such fix-ups are
    recorded as human-readable notes under ``cfg['_warnings']`` — see
    :func:`get_config_warnings`.
    """
    cfg: dict = {}
    warnings: list[str] = []
    if _CONFIG_PATH.exists():
        cfg, warnings = _load_toml_lenient(_CONFIG_PATH)

    # Schwab overrides
    schwab_keys = {
        "app_key": os.environ.get("STOCKPILE_SCHWAB_APP_KEY"),
        "app_secret": os.environ.get("STOCKPILE_SCHWAB_APP_SECRET"),
        "callback_url": os.environ.get("STOCKPILE_SCHWAB_CALLBACK_URL"),
        "token_file": os.environ.get("STOCKPILE_SCHWAB_TOKEN_FILE"),
    }
    if any(v is not None for v in schwab_keys.values()):
        s = cfg.setdefault("schwab", {})
        for k, v in schwab_keys.items():
            if v is not None:
                s[k] = v

    # Validate the paper flag when a [schwab] section parsed intact.
    if isinstance(cfg.get("schwab"), dict):
        _normalize_paper(cfg["schwab"], warnings)

    if warnings:
        cfg["_warnings"] = warnings
    return cfg


def get_config_warnings(cfg: dict) -> list[str]:
    """Human-readable notes recorded while loading config (a malformed file,
    a bad ``paper`` flag, …). Empty list when the config loaded cleanly."""
    return list(cfg.get("_warnings", []))


def get_provider(cfg: dict) -> str:
    """Return 'yahoo', 'schwab', or 'moomoo' from config, defaulting to 'yahoo'."""
    return cfg.get("data_source", {}).get("provider", "yahoo")


def get_schwab_config(cfg: dict) -> dict:
    """Return the [schwab] section with defaults filled in."""
    s = cfg.get("schwab", {})
    return {
        "app_key":      s.get("app_key", ""),
        "app_secret":   s.get("app_secret", ""),
        "callback_url": s.get("callback_url", "https://127.0.0.1:8182/"),
        "token_file":   s.get("token_file", "~/.config/schwab-token.json"),
        # Forward-looking: when order placement is enabled, default to
        # paper/sandbox so live orders are an explicit opt-in.
        "paper":        bool(s.get("paper", True)),
    }


def get_moomoo_config(cfg: dict) -> dict:
    """Return the [moomoo] section with defaults filled in.

    Keys:
        host: IP address of the OpenD gateway (default '127.0.0.1').
        port: TCP port of the OpenD gateway (default 11111).
    """
    m = cfg.get("moomoo", {})
    return {
        "host": m.get("host", "127.0.0.1"),
        "port": int(m.get("port", 11111)),
    }
