"""Hermes plugin for Ponytail."""

from __future__ import annotations

import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

DEFAULT_MODE = "full"
RUNTIME_MODES = {"off", "lite", "full", "ultra"}
SKILL_COMMANDS = {
    "ponytail-review": "Review the current diff or provided target: bugs, security, load, missing tests, speed, and what to cut.",
    "ponytail-audit": "Audit the whole repo: bugs, security, load, missing tests, speed, and what to cut.",
    "ponytail-debt": "List every deliberate `shortcut:` comment and its upgrade path.",
    "ponytail-gain": "Show the measured-impact scoreboard (less code, less cost, more speed).",
    "ponytail-help": "Show the Ponytail command reference.",
}

ROOT = Path(__file__).resolve().parent
SKILLS_DIR = ROOT / "skills"
PONYTAIL_SKILL = SKILLS_DIR / "ponytail" / "SKILL.md"
HERMES_ADDENDUM = ROOT / "hermes-addendum.md"

_current_mode = None  # process-wide fallback when a host passes no session id

# Fork (seppegadeyne/ponytail): activation and modes are per session, not per process,
# so one /ponytail in a multiplexed gateway does not switch every chat at once.
ACTIVATIONS = {"auto", "always", "manual"}
DEFAULT_SKIP_SOURCES = "cron,tool"
_MAX_SESSIONS = 512
_ctx: Any = None
_lock = threading.Lock()
_session_modes: "OrderedDict[str, str]" = OrderedDict()  # set by /ponytail in that session
_session_state: "OrderedDict[str, dict[str, Any]]" = OrderedDict()  # last decision per session

_MARKER_RE = re.compile(r"PONYTAIL MODE (?:ACTIVE — level: ([a-z]+)|OFF)")
_CODE_EXT = (
    r"py|pyi|js|mjs|cjs|jsx|ts|tsx|php|go|rs|rb|java|kt|swift|c|cc|cpp|h|hpp|cs|vue|svelte|astro"
    r"|css|scss|sh|bash|sql|lua|dart|ex|exs|scala|zig|toml"
)
_CODE_FILE_RE = re.compile(rf"\.(?:{_CODE_EXT})$", re.I)
# Dev intent in a user message or a subagent goal. Conservative on purpose: a miss is cheap
# (the tool-call signal still activates once code is touched), a false hit costs ~700 tokens once.
_DEV_INTENT_RE = re.compile(
    rf"\b[\w./-]+\.(?:{_CODE_EXT})\b|github\.com/[\w.-]+/[\w.-]+/(?:pull|issues|commit|blob|compare)/"
    r"|\b(?:pull request|PR ?#?\d+|diff|commit\w*|refactor\w*|bug ?fix\w*|stack ?trace|traceback"
    r"|pytest|vitest|jest|unit ?tests?|test ?suite|lint\w*|codebase|code ?review|implement\w*|debug\w*|compile\w*)\b",
    re.I,
)
# Mid-session signal: only real code work. git subcommands and reads are left out on purpose:
# they also run in CV, wiki and ops repos and during research (replay 2026-10-29: most false hits).
_DEV_COMMAND_RE = re.compile(
    r"\b(?:pytest|vitest|jest|npm (?:run )?test|pnpm (?:run )?test|cargo (?:test|build)|go test|tsc|eslint|ruff|mypy)\b"
)
_CODE_TOOLS = {"write_file", "patch"}


def _normalize_runtime_mode(mode: str | None) -> str | None:
    if not isinstance(mode, str):
        return None
    mode = mode.strip().lower()
    return mode if mode in RUNTIME_MODES else None


def _config_dir() -> Path:
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]) / "ponytail"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ponytail"
    return Path.home() / ".config" / "ponytail"


def _setting(key: str) -> Any:
    """``plugins.entries.ponytail.settings.<key>`` from config.yaml; None outside Hermes."""
    if _ctx is None:
        return None
    try:
        return _ctx.get_config(key)
    except Exception:
        return None


def _activation() -> str:
    for raw in (_setting("activation"), os.environ.get("PONYTAIL_ACTIVATION")):
        if isinstance(raw, str) and raw.strip().lower() in ACTIVATIONS:
            return raw.strip().lower()
    return "auto"


def _skip_sources() -> set[str]:
    raw = _setting("skip_sources")
    if raw is None:
        raw = os.environ.get("PONYTAIL_SKIP_SOURCES", DEFAULT_SKIP_SOURCES)
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    return {str(item).strip().lower() for item in items if str(item).strip()}


def _default_mode() -> str:
    setting_mode = _normalize_runtime_mode(_setting("default_mode"))
    if setting_mode:
        return setting_mode
    env_mode = _normalize_runtime_mode(os.environ.get("PONYTAIL_DEFAULT_MODE"))
    if env_mode:
        return env_mode
    try:
        data = json.loads((_config_dir() / "config.json").read_text(encoding="utf-8-sig"))
        file_mode = _normalize_runtime_mode(data.get("defaultMode"))
        if file_mode:
            return file_mode
    except Exception:
        pass
    return DEFAULT_MODE


def _strip_frontmatter(text: str) -> str:
    return re.sub(r"^---[\s\S]*?---\s*", "", text or "", count=1)


def _filter_skill_body_for_mode(body: str, mode: str) -> str:
    effective = _normalize_runtime_mode(mode) or DEFAULT_MODE
    lines = []
    # str.splitlines() drops a trailing line terminator instead of yielding a
    # trailing empty element, so a body ending in "\n" loses that newline on
    # rejoin. re.split keeps it, matching the JS filter's split(/\r?\n/).
    for line in re.split(r"\r?\n", _strip_frontmatter(body)):
        table_label = re.match(r"^\|\s*\*\*(.+?)\*\*\s*\|", line)
        if table_label:
            label_mode = _normalize_runtime_mode(table_label.group(1))
            if label_mode and label_mode != effective:
                continue

        # Require a quoted value: every worked example is `- lite: "..."`. Without
        # this, an ordinary rule bullet that happens to start with a mode word
        # (e.g. "- Full: ...") is silently dropped in every other mode — it looks
        # like a worked example but is really prose meant to survive verbatim.
        # Mirrors the fix in hooks/ponytail-instructions.js (#571).
        example_label = re.match(r'^-\s*([^:]+):\s*"', line)
        if example_label:
            label_mode = _normalize_runtime_mode(example_label.group(1))
            if label_mode and label_mode != effective:
                continue

        lines.append(line)
    return "\n".join(lines)


def _fallback_instructions(mode: str) -> str:
    return (
        f"PONYTAIL MODE ACTIVE — level: {mode}\n\n"
        "You are a lazy senior developer. The best code is the code never written. "
        "Before you write, list every place your change must reach (callers, tests, "
        "fixtures) and what it could break for users. First rung that works: skip what "
        "is not needed, reuse what the codebase has, stdlib or platform, installed "
        "dependency, one readable line, then minimum code. No unrequested abstractions, "
        "wrappers, options or boilerplate. Never cut trust-boundary validation, "
        "data-loss handling, security, accessibility or anything asked for."
    )


def build_injected_context(mode: str | None = None) -> str:
    """Return the mode-filtered Ponytail context injected before LLM turns."""
    effective = _normalize_runtime_mode(mode) or _default_mode()
    if effective == "off":
        return ""
    try:
        body = PONYTAIL_SKILL.read_text(encoding="utf-8")
        context = f"PONYTAIL MODE ACTIVE — level: {effective}\n\n{_filter_skill_body_for_mode(body, effective)}"
    except OSError:
        context = _fallback_instructions(effective)
    addendum = _addendum()
    return f"{context.rstrip()}\n\n{addendum}" if addendum else context


def _addendum() -> str:
    """Hermes house rules appended after the upstream ruleset (setting ``addendum: false`` drops them)."""
    if _setting("addendum") is False:
        return ""
    try:
        return _strip_frontmatter(HERMES_ADDENDUM.read_text(encoding="utf-8")).strip()
    except OSError:
        return ""


def _remember(table: "OrderedDict[str, Any]", key: str, value: Any) -> None:
    with _lock:
        table[key] = value
        table.move_to_end(key)
        while len(table) > _MAX_SESSIONS:
            table.popitem(last=False)


def _last_marker(conversation_history: Any) -> str | None:
    """Mode last delivered to the model in this conversation ('off' included), else None.

    Hermes replays hook context from the user row's ``api_content``; the tool-call activation
    lands in a tool result's ``content``. Both survive restarts, so they are the source of truth.
    """
    for message in reversed(conversation_history or []):
        if not isinstance(message, dict):
            continue
        for field in ("api_content", "content"):
            text = message.get(field)
            if field == "content" and message.get("role") != "tool":
                continue
            if isinstance(text, str):
                matches = _MARKER_RE.findall(text)
                if matches:
                    return matches[-1] or "off"
    return None


def _in_git_repo(path: str | None) -> bool:
    if not path:
        return False
    try:
        current = Path(path).expanduser().resolve()
        home = Path.home().resolve()
    except Exception:
        return False
    for candidate in (current, *current.parents):
        if candidate == home or candidate == candidate.parent:
            return False
        if (candidate / ".git").exists():
            return True
    return False


def _is_dev_session(user_message: Any) -> bool:
    """Auto activation: dev intent in the message/goal, or Hermes started inside a git repo."""
    if isinstance(user_message, str) and _DEV_INTENT_RE.search(user_message):
        return True
    return _in_git_repo(os.environ.get("TERMINAL_CWD") or os.getcwd())


def _source_skipped(platform: str) -> bool:
    sources = {str(platform or "").strip().lower(), os.environ.get("HERMES_SESSION_SOURCE", "").strip().lower()}
    return bool(sources & _skip_sources())


def _pre_llm_call(
    session_id: str = "", conversation_history: Any = None, user_message: Any = None,
    platform: str = "", **_: Any
) -> dict[str, str] | None:
    sid = str(session_id or "")
    last = _last_marker(conversation_history)
    explicit = (_session_modes.get(sid) if sid else None) or _current_mode
    if explicit:
        mode = explicit
    elif last:
        mode = last  # once a session is active it stays active; auto never switches it off
    elif _source_skipped(platform) or _activation() == "manual":
        mode = None
    elif _activation() == "always" or _is_dev_session(user_message):
        mode = _default_mode()
    else:
        mode = None
    if sid:
        _remember(_session_state, sid, {"mode": mode or last, "platform": str(platform or "")})
    if mode is None or mode == last:
        return None
    if mode == "off":
        # Off is injected too: an earlier ruleset is replayed every turn, and the
        # /ponytail off reply goes to the user, never to the model. Nothing persisted: nothing to undo.
        return {"context": "PONYTAIL MODE OFF"} if last else None
    return {"context": build_injected_context(mode)}


def _touches_code(tool_name: str, args: Any) -> bool:
    """An edit to a source file inside a git repo, or a test/build/lint run."""
    if not isinstance(args, dict):
        return False
    if tool_name in _CODE_TOOLS:
        target = str(args.get("path") or "")
        if not _CODE_FILE_RE.search(target):
            return False
        path = Path(target).expanduser()
        if not path.is_absolute():
            path = Path(os.environ.get("TERMINAL_CWD") or os.getcwd()) / path
        return _in_git_repo(str(path.parent))
    if tool_name == "terminal":
        return bool(_DEV_COMMAND_RE.search(str(args.get("command") or "")))
    return False


def _transform_tool_result(
    tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "",
    turn_id: str = "", **_: Any
) -> str | None:
    """Mid-session activation: the first code-touching tool call in an inactive session carries the
    ruleset once. Fail-open; ignores nested calls (execute_code scripts parse results as JSON)."""
    try:
        sid = str(session_id or "")
        if not sid or not turn_id or not isinstance(result, str) or _activation() != "auto":
            return None
        state = _session_state.get(sid)
        if state is None or state.get("mode") or _source_skipped(state.get("platform", "")):
            return None
        if not _touches_code(str(tool_name or ""), args):
            return None
        mode = _default_mode()
        if mode == "off":
            return None
        _remember(_session_state, sid, {**state, "mode": mode})
        return f"{result}\n\n[ponytail: coding work detected, rules active from here on]\n{build_injected_context(mode)}"
    except Exception:
        return None


def _skill_prompt(command: str, args: str = "") -> str:
    tail = args.strip()
    target = f"\n\nUser arguments: {tail}" if tail else ""
    return (
        f"Load and follow the Hermes plugin skill `ponytail:{command}`. "
        f"{SKILL_COMMANDS[command]}{target}"
    )


def _slash_access_denied(event: Any, gateway: Any, command: str) -> bool:
    if gateway is None or event is None:
        return False
    checker = getattr(gateway, "_check_slash_access", None)
    source = getattr(event, "source", None)
    if checker is None or source is None:
        return False
    try:
        return checker(source, command) is not None
    except Exception:
        return True


def rewrite_gateway_command(event: Any = None, gateway: Any = None, **_: Any) -> dict[str, str] | None:
    """Rewrite authorized gateway /ponytail-* commands into normal agent prompts."""
    text = str(getattr(event, "text", "") or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text[1:].partition(" ")
    command = head.replace("_", "-").lower()
    if command not in SKILL_COMMANDS:
        return None
    if _slash_access_denied(event, gateway, command):
        return None
    return {"action": "rewrite", "text": _skill_prompt(command, rest)}


def _handle_mode_command(raw_args: str, session_id: str = "", **_: Any) -> str:
    global _current_mode
    sid = str(session_id or "")
    arg = (raw_args or "").strip().lower()
    if sid:
        current = _session_modes.get(sid) or (_session_state.get(sid) or {}).get("mode")
    else:
        current = _current_mode or _default_mode()
    if not arg:
        # Bare /ponytail switches ponytail on, or reports the level when it already is (#639).
        if current and current != "off":
            return f"Ponytail mode: {current}. Use `/ponytail lite|full|ultra|off`."
        arg = "full" if _default_mode() == "off" else _default_mode()
    mode = _normalize_runtime_mode(arg)
    if not mode:
        return "Usage: /ponytail [lite|full|ultra|off]"
    if sid:
        _remember(_session_modes, sid, mode)
    else:
        _current_mode = mode
    return f"Ponytail mode set to {mode}."


def _make_skill_command_handler(ctx: Any, command: str) -> Callable[[str], str]:
    def handler(raw_args: str) -> str:
        prompt = _skill_prompt(command, raw_args or "")
        injected = False
        try:
            injected = bool(ctx.inject_message(prompt))
        except Exception:
            injected = False
        if injected:
            return f"Queued `{command}` for the agent."
        return prompt

    return handler


def register(ctx: Any) -> None:
    """Register Ponytail hooks, skills, and slash commands with Hermes."""
    global _ctx
    _ctx = ctx if hasattr(ctx, "get_config") else None
    for child in sorted(SKILLS_DIR.iterdir() if SKILLS_DIR.exists() else []):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)

    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_gateway_dispatch", rewrite_gateway_command)
    ctx.register_hook("transform_tool_result", _transform_tool_result)

    ctx.register_command(
        "ponytail",
        _handle_mode_command,
        description="Set Ponytail lazy senior dev mode: lite, full, ultra, or off.",
        args_hint="[lite|full|ultra|off]",
    )
    for command, description in SKILL_COMMANDS.items():
        ctx.register_command(
            command,
            _make_skill_command_handler(ctx, command),
            description=description,
            args_hint="[target or notes]",
        )
