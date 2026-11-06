"""Hermes plugin ``rex``: make Hermes Rex-aware.

Rex (https://www.superlogical.com/rex) is a terminal multiplexer. When Hermes
runs inside a Rex pane this plugin:

1. Reports program status. Lifecycle hooks become OSC 7501 reports
   (https://www.superlogical.com/rex/docs/build/program-status), so Rex shows the
   agent as working, blocked (permission / question / auth) or done and sends
   its Done notification.
2. Injects session context. A system prompt section tells the model which Rex
   session and pane it runs in and how to use the ``rex`` CLI; the text is what
   ``rex __internal session-start`` prints, so it matches the installed Rex.
3. Registers the ``rex`` skill (``rex:rex``), so the model can load the full CLI
   guide on demand.

Finding the pane
  Hermes runs every CLI and TUI turn in its gateway daemon, so hooks fire in a
  process with no terminal. The plugin maps a session to the pane's tty, in
  order of preference:

  a. The session's forwarded client environment, when config.yaml has
         terminal:
           client_env_passthrough: ["REX_BLOCK", "REX_SESSION", "REX_SERVER", "REX_BIN_DIR"]
     (a documented Hermes setting). Exact, and it also puts the ids in the
     terminal tool's environment. ``hermes rex doctor --fix`` adds it.
  b. Hermes's classic-CLI terminal breadcrumbs (~/.hermes/terminal-sessions),
     which map a tty to its session id.
  c. The TUI's active-session file: each TUI front end carries
     HERMES_TUI_ACTIVE_SESSION_FILE in its environment, a file holding the
     session id, and ``ps`` gives that process's tty. Exact for any number of TUIs.
  d. The controlling tty of the single running TUI, as a last resort.

  With b, c or d the Rex ids for the context block are found the other way
  round, by asking Rex which pane runs on that tty. So nothing needs
  configuring; the setting is a precision and convenience upgrade.

Nothing is written to stdout. Sessions that did not come from a terminal
(Telegram, Discord, cron, ...) are ignored.

Settings (``plugins.entries.rex.settings`` in config.yaml, see config_schema)
  session_naming: false   # let the model rename the Rex session after its task
  app_label: hermes       # ``app`` id in status reports
  debug: false            # hook trace in <plugin data dir>/debug.log

Subprocesses run: ``rex`` and ``ps``. No network. No credentials read.

One private seam: ``agent.session_client_env.bound_client_env`` (used in-tree,
not yet a public plugin API; hermes-agent issue #141194 asks for one). Guarded:
if it goes away, path a is skipped and b-d still work.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time

_TITLE = "Hermes"
_INTERACTIVE_PLATFORMS = {"cli", "tui", "desktop", "acp", "local"}
_MSG_LIMIT = 160
_KINDS = {"approval": "permission", "sudo": "auth", "clarify": "question"}
_HOME = os.path.dirname(os.path.abspath(__file__))
_SKILL = os.path.join(_HOME, "skills", "rex", "SKILL.md")
_HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
_BREADCRUMBS = os.path.join(_HERMES_HOME, "terminal-sessions")
_CONFIG = os.path.join(_HERMES_HOME, "config.yaml")
_REX_ENV = ("REX_BLOCK", "REX_SESSION", "REX_SERVER", "REX_BIN_DIR")
_TUI_MARKER = "ui-tui/dist/entry.js"
_DEBUG = bool(os.environ.get("REX_PLUGIN_DEBUG"))

_lock = threading.Lock()
_session_tty: dict[str, tuple[str | None, float]] = {}
_block_tty: dict[str, str] = {}
_tty_ids: dict[str, tuple[dict, float]] = {}
_tui_map: tuple[dict[str, str], float] = ({}, 0.0)
_last_reply: dict[str, str] = {}
_settings = {"session_naming": False, "app_label": "hermes", "debug": False}


# --- plumbing -----------------------------------------------------------------

def _data_dir() -> str:
    """Runtime state never goes into the plugin directory (catalog rule); use Hermes's data dir."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return str(plugin_data_dir("rex"))
    except Exception:
        path = os.path.join(_HERMES_HOME, "plugin-data", "rex")
        os.makedirs(path, exist_ok=True)
        return path


def _debug(line: str) -> None:
    if not (_DEBUG or _settings["debug"]):
        return
    try:
        with open(os.path.join(_data_dir(), "debug.log"), "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {line}\n")
    except OSError:
        pass


def _run(cmd: list[str], timeout: float = 3, env: dict | None = None) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env).stdout
    except Exception as exc:
        _debug(f"command failed {cmd[:3]}: {exc!r}")
        return ""


def _tty_of_pid(pid: int) -> str | None:
    name = _run(["ps", "-o", "tty=", "-p", str(pid)]).strip()
    if not name or name == "??":
        return None
    path = "/dev/" + name
    return path if os.path.exists(path) else None


def _client_env() -> dict:
    """The session's forwarded client environment (daemon), else this process's own."""
    try:
        # Private seam (see module docstring); guarded so its removal only costs precision.
        from agent.session_client_env import bound_client_env
        env = bound_client_env()
        if isinstance(env, dict):
            return env
    except Exception as exc:
        _debug(f"client env unavailable: {exc!r}")
    return dict(os.environ)


# --- rex CLI --------------------------------------------------------------------

_REX_DEFAULT = os.path.expanduser("~/Library/Application Support/com.superlogical.rex/RexServer/Versions/Current/"
                                  "Rex Server.app/Contents/MacOS/rex")


def _rex_binary(env: dict) -> str:
    bin_dir = env.get("REX_BIN_DIR")
    if bin_dir and os.path.exists(os.path.join(bin_dir, "rex")):
        return os.path.join(bin_dir, "rex")
    found = shutil.which("rex")
    if found:
        return found
    return _REX_DEFAULT if os.path.exists(_REX_DEFAULT) else "rex"


def _rex_env(env: dict, **overrides: str) -> dict:
    """An environment for running the rex CLI as if from the pane."""
    merged = dict(os.environ)
    for name in _REX_ENV:
        if env.get(name):
            merged[name] = env[name]
    merged.update({k: v for k, v in overrides.items() if v})
    if env.get("REX_BIN_DIR"):
        merged["PATH"] = env["REX_BIN_DIR"] + os.pathsep + merged.get("PATH", "")
    return merged


def _rex(env: dict, args: list[str], timeout: float = 3, **overrides: str) -> str:
    cmd = [_rex_binary(env)]
    if env.get("REX_SERVER"):
        cmd += ["-S", env["REX_SERVER"]]
    return _run(cmd + args, timeout=timeout, env=_rex_env(env, **overrides))


def _block_tty_lookup(env: dict, block: str) -> str | None:
    if block in _block_tty:
        return _block_tty[block]
    tty = None
    try:
        info = json.loads(_rex(env, ["block", "call", "-b", block, "process"]) or "{}")
        pid = (info.get("child") or {}).get("pid") or (info.get("foreground") or {}).get("pid")
        if pid:
            tty = _tty_of_pid(int(pid))
    except Exception as exc:
        _debug(f"rex block lookup failed: {exc!r}")
    if tty:
        _block_tty[block] = tty  # a pane's pty never changes
    return tty


_ENV_RE = re.compile(r"(REX_(?:BLOCK|SESSION|SERVER))=(\S+)")
_BIN_RE = re.compile(r"REX_BIN_DIR=(.*?)(?= [A-Za-z_][A-Za-z0-9_]*=|$)")


def parse_rex_env(ps_command_line: str) -> dict:
    """The Rex variables in one ``ps -o command= -E`` line (REX_BIN_DIR may contain spaces)."""
    env = {k: v for k, v in _ENV_RE.findall(ps_command_line)}
    m = _BIN_RE.search(ps_command_line)
    if m:
        env["REX_BIN_DIR"] = m.group(1).strip()
    return env


def process_env(pid: str, wanted: tuple[str, ...]) -> dict:
    """Selected environment variables of one of the user's own processes.
    Linux: /proc/<pid>/environ (exact, NUL-separated). macOS/BSD: ``ps -E``, parsed."""
    proc = f"/proc/{pid}/environ"
    if os.path.exists(proc):
        try:
            with open(proc, "rb") as f:
                items = f.read().split(b"\0")
            env = {}
            for item in items:
                k, sep, v = item.partition(b"=")
                if sep and k.decode(errors="replace") in wanted:
                    env[k.decode()] = v.decode(errors="replace")
            return env
        except OSError:
            return {}
    line = _run(["ps", "-o", "command=", "-E", "-p", str(pid)])
    env = parse_rex_env(line)
    for name in wanted:
        if name not in env and not name.startswith("REX_"):
            m = re.search(re.escape(name) + r"=(\S+)", line)
            if m:
                env[name] = m.group(1)
    return {k: v for k, v in env.items() if k in wanted}


def _pane_env_for_tty(tty: str) -> dict:
    """Rex ids of the pane on ``tty``: every process in a Rex pane inherits REX_SESSION and REX_BLOCK,
    so read them from any process on that tty (own user only). Cached per tty; a miss retries after 10s."""
    cached = _tty_ids.get(tty)
    if cached and (cached[0] or time.time() - cached[1] < 10):
        return cached[0]
    found: dict = {}
    for pid in _run(["ps", "-t", tty.replace("/dev/", ""), "-o", "pid="]).split():
        env = process_env(pid, _REX_ENV)
        if env.get("REX_BLOCK") and env.get("REX_SESSION"):
            found = env
            break
    _tty_ids[tty] = (found, time.time())
    _debug(f"tty {tty} -> pane env {sorted(found)}")
    return found


# --- session -> tty --------------------------------------------------------------

def breadcrumb_tty(name: str) -> str:
    """``tty-dev-ttys004`` -> ``/dev/ttys004`` (the breadcrumb file naming scheme)."""
    return "/" + name[len("tty-"):].replace("-", "/")


def _tty_from_breadcrumbs(session_id: str) -> str | None:
    try:
        names = os.listdir(_BREADCRUMBS)
    except OSError:
        return None
    for name in names:
        if not name.startswith("tty-"):
            continue
        try:
            with open(os.path.join(_BREADCRUMBS, name)) as f:
                if json.load(f).get("session_id") != session_id:
                    continue
        except Exception:
            continue
        tty = breadcrumb_tty(name)
        if os.path.exists(tty):
            return tty
    return None


def parse_active_session_file(text: str) -> str | None:
    """The session id inside a TUI active-session file (``{"session_id": ...}``)."""
    try:
        value = json.loads(text).get("session_id")
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def _tui_sessions() -> dict[str, str]:
    """session_id -> tty for every running TUI front end, from its environment's
    HERMES_TUI_ACTIVE_SESSION_FILE. Refreshed every 15 seconds."""
    global _tui_map
    now = time.time()
    if now - _tui_map[1] < 15:
        return _tui_map[0]
    result: dict[str, str] = {}
    for line in _run(["ps", "-axo", "pid=,tty=,command="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or _TUI_MARKER not in parts[2] or parts[1] == "??":
            continue
        pid, tty = parts[0], "/dev/" + parts[1]
        path = process_env(pid, ("HERMES_TUI_ACTIVE_SESSION_FILE",)).get("HERMES_TUI_ACTIVE_SESSION_FILE")
        if not path:
            continue
        try:
            with open(path) as f:
                sid = parse_active_session_file(f.read())
        except OSError:
            sid = None
        if sid and os.path.exists(tty):
            result[sid] = tty
    _tui_map = (result, now)
    return result


def _tty_from_single_tui() -> str | None:
    ttys = {line.split(None, 1)[0] for line in _run(["ps", "-axo", "tty=,command="]).splitlines()
            if _TUI_MARKER in line and not line.startswith("??")}
    if len(ttys) != 1:
        return None
    cand = "/dev/" + ttys.pop()
    return cand if os.path.exists(cand) else None


def _tty_for(session_id: object, platform: object, force: bool = False) -> str | None:
    if not isinstance(session_id, str) or not session_id:
        return None
    now = time.time()
    cached = _session_tty.get(session_id)
    if cached and not force and now - cached[1] < 5:
        return cached[0]
    env = _client_env()
    tty, how = None, "none"
    if env.get("REX_BLOCK"):
        tty, how = _block_tty_lookup(env, env["REX_BLOCK"]), "client-env"
    if tty is None:
        tty, how = _tty_from_breadcrumbs(session_id), "breadcrumb"
    if tty is None:
        tty, how = _tui_sessions().get(session_id), "tui-session-file"
    if tty is None and platform == "tui":
        tty, how = _tty_from_single_tui(), "single-tui"
    _session_tty[session_id] = (tty, now)
    _debug(f"session {session_id[-12:]} -> {tty} via {how}")
    return tty


# --- status reports -----------------------------------------------------------

def one_line(text: object, limit: int = _MSG_LIMIT) -> str:
    s = str(text or "")
    s = s.strip().splitlines()[0] if s.strip() else ""
    s = "".join(ch for ch in s if ch.isprintable() and ch not in "\t")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def osc7501(state: str, *, app: str, kind: str | None = None, msg: str | None = None,
            title: str | None = None, progress: int | None = None) -> bytes:
    """One Program Status report. Returns b"" when it would exceed the protocol's size cap."""
    pairs = [f"state={state}", f"app={app}"]
    if kind:
        pairs.append(f"kind={kind}")
    if progress is not None:
        pairs.append(f"progress={max(0, min(100, int(progress)))}")
    if title:
        pairs.append(f"title={_b64(one_line(title, 64))}")
    if msg:
        pairs.append(f"msg={_b64(one_line(msg))}")
    data = ("\x1b]7501;" + ":".join(pairs) + "\x1b\\").encode("utf-8")
    return data if len(data) <= 4000 else b""


def _report(state: str, kwargs: dict, *, kind: str | None = None, msg: object = None) -> None:
    platform = kwargs.get("platform")
    if platform and platform not in _INTERACTIVE_PLATFORMS:
        return
    tty = _tty_for(kwargs.get("session_id"), platform)
    _debug(f"report state={state} kind={kind} tty={tty} platform={platform!r}")
    if not tty:
        return
    data = osc7501(state, app=_settings["app_label"], kind=kind, msg=one_line(msg) or None, title=_TITLE)
    if not data:
        return
    with _lock:
        try:
            fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY)
        except OSError as exc:
            _debug(f"tty open failed: {exc}")
            return
        try:
            os.write(fd, data)
        except OSError as exc:
            _debug(f"tty write failed: {exc}")
        finally:
            os.close(fd)


def _idle(**kwargs) -> None:
    _report("idle", kwargs)


def _thinking(**kwargs) -> None:
    _report("working", kwargs, msg="thinking")


def _tool(tool_name: str = "", **kwargs) -> None:
    _report("working", kwargs, msg=tool_name or "tool call")


def _approval(command: str = "", description: str = "", **kwargs) -> None:
    _report("blocked", kwargs, kind="permission", msg=description or command or "approval needed")


def _human_input(kind: str = "", prompt: str = "", **kwargs) -> None:
    _report("blocked", kwargs, kind=_KINDS.get(kind, "question"), msg=prompt or kind or "input needed")


def _resume(**kwargs) -> None:
    _report("working", kwargs, msg="thinking")


def _reply(assistant_response: object = "", **kwargs) -> None:
    sid = kwargs.get("session_id")
    line = one_line(assistant_response)
    if isinstance(sid, str) and line:
        _last_reply[sid] = line
    _report("done", kwargs, msg=line or "finished")


def _turn_end(completed: bool = True, failed: bool = False, interrupted: bool = False,
              turn_exit_reason: str = "", **kwargs) -> None:
    sid = kwargs.get("session_id")
    if interrupted:
        _report("idle", kwargs)
    elif failed:
        _report("error", kwargs, msg=turn_exit_reason or "turn failed")
    elif completed:
        _report("done", kwargs, msg=_last_reply.pop(sid, None) or "finished")


def _stopped(**kwargs) -> None:
    _report("idle", kwargs)


def _delegate(child_role: object = None, **kwargs) -> None:
    role = f": {child_role}" if child_role else ""
    _report("working", kwargs, msg=f"delegating{role}")


_HOOKS = (
    ("on_session_start", _idle),
    ("on_session_reset", _idle),
    ("pre_llm_call", _thinking),
    ("pre_tool_call", _tool),
    ("post_tool_call", _thinking),
    ("pre_approval_request", _approval),
    ("post_approval_response", _resume),
    ("on_human_input_request", _human_input),
    ("on_human_input_resolved", _resume),
    ("post_llm_call", _reply),
    ("on_session_end", _turn_end),
    ("agent_loop_stopped", _stopped),
    ("subagent_start", _delegate),
    ("subagent_stop", _thinking),
)


# --- session context (system prompt section) ---------------------------------

_HERMES_NOTE = (
    "\nIn Hermes, the Rex guide is the `rex:rex` skill: load it with skill_view when a task "
    "involves Rex sessions, tabs, panes or terminal output. The `rex` command is available in "
    "the terminal tool; if REX_SESSION and REX_BLOCK are not set there, pass the ids above "
    "with -s and -b.\n"
)


def rex_context(info=None) -> str:
    """Text for the system prompt section: what ``rex __internal session-start`` prints for this
    session's pane, plus a Hermes-specific note. Empty (section skipped) outside Rex."""
    info = info or {}
    env = _client_env()
    session, block = env.get("REX_SESSION"), env.get("REX_BLOCK")
    if not (session and block):
        tty = _tty_for(info.get("session_id"), info.get("platform"), force=True)
        pane = _pane_env_for_tty(tty) if tty else {}
        if not (pane.get("REX_SESSION") and pane.get("REX_BLOCK")):
            _debug("context: no Rex pane found for this session; section skipped")
            return ""
        env = {**env, **pane}
        session, block = pane["REX_SESSION"], pane["REX_BLOCK"]
    args = ["__internal", "session-start", "--agent", "hermes"]
    if not _settings["session_naming"]:
        args.append("--feature-session-naming=false")
    text = _rex(env, args, timeout=5, REX_SESSION=session, REX_BLOCK=block).strip()
    if not text:
        _debug("context: session-start produced nothing")
        return ""
    return (text + "\n" + _HERMES_NOTE)[:3900]


# --- hermes rex doctor --------------------------------------------------------

_PASSTHROUGH_LINE = '  client_env_passthrough: ["REX_BLOCK", "REX_SESSION", "REX_SERVER", "REX_BIN_DIR"]'


def _doctor_setup(parser) -> None:
    sub = parser.add_subparsers(dest="rex_cmd")
    doctor = sub.add_parser("doctor", help="Check that Hermes and Rex are wired up")
    doctor.add_argument("--fix", action="store_true",
                        help="add terminal.client_env_passthrough for the Rex ids to the Hermes config.yaml")


def _passthrough_names() -> set[str]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        cfg = {}
    return set((cfg.get("terminal") or {}).get("client_env_passthrough") or [])


def _fix_passthrough() -> str:
    """Add the Rex ids to terminal.client_env_passthrough in config.yaml. Returns a message."""
    try:
        text = open(_CONFIG).read()
    except OSError as exc:
        return f"cannot read {_CONFIG}: {exc}"
    lines = text.splitlines(keepends=True)
    backup = f"{_CONFIG}.bak-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    shutil.copy(_CONFIG, backup)
    for i, line in enumerate(lines):
        if line.startswith("terminal:"):
            # replace an existing key inside this block, else insert right after the header
            j = i + 1
            while j < len(lines) and (lines[j].startswith(" ") or not lines[j].strip() or lines[j].startswith("#")):
                if lines[j].lstrip().startswith("client_env_passthrough:"):
                    existing = lines[j].split(":", 1)[1].split("#", 1)[0].strip().strip("[]")
                    names = [n.strip().strip("\"'") for n in existing.split(",") if n.strip()]
                    merged = names + [n for n in _REX_ENV if n not in names]
                    lines[j] = "  client_env_passthrough: [" + ", ".join(f'"{n}"' for n in merged) + "]\n"
                    open(_CONFIG, "w").write("".join(lines))
                    return f"updated client_env_passthrough in {_CONFIG} (backup: {backup})"
                j += 1
            lines.insert(i + 1, _PASSTHROUGH_LINE + "\n")
            open(_CONFIG, "w").write("".join(lines))
            return f"added client_env_passthrough under terminal: in {_CONFIG} (backup: {backup})"
    lines.append("\nterminal:\n" + _PASSTHROUGH_LINE + "\n")
    open(_CONFIG, "w").write("".join(lines))
    return f"added a terminal: section with client_env_passthrough to {_CONFIG} (backup: {backup})"


def _doctor(args=None) -> int:
    rows: list[tuple[bool, str, str]] = []  # (ok, text, fix)

    def check(ok: bool, text: str, fix: str = "") -> None:
        rows.append((ok, text, fix))

    in_rex = os.environ.get("TERM_PROGRAM") == "rex" or bool(os.environ.get("REX_BLOCK"))
    check(in_rex, "Running inside a Rex pane" if in_rex else "Not inside a Rex pane",
          "" if in_rex else "run `hermes rex doctor` from a Rex terminal to check the full path")
    rex = shutil.which("rex")
    check(bool(rex), f"rex CLI on PATH ({rex})" if rex else "rex CLI not on PATH",
          "" if rex else "open a Rex terminal, or add Rex's bin directory to PATH")
    if rex:
        who = _run([rex, "whoami", "--json"], timeout=5).strip()
        check(bool(who), "rex whoami answers (server reachable)" if who else "rex whoami failed",
              "" if who else "is the Rex server running?")
    names = _passthrough_names()
    missing = [n for n in _REX_ENV if n not in names]
    fixed = ""
    if missing and getattr(args, "fix", False):
        fixed = _fix_passthrough()
        missing = [n for n in _REX_ENV if n not in _passthrough_names()]
    check(not missing, "terminal.client_env_passthrough forwards the Rex ids (exact mode)" if not missing
          else "terminal.client_env_passthrough does not forward " + ", ".join(missing) + " (fallback mode)",
          "" if not missing else "optional; run `hermes rex doctor --fix` or add to " + _CONFIG + ":\n"
          + "      terminal:\n    " + _PASSTHROUGH_LINE)
    pid_file = os.path.join(_HERMES_HOME, "gateway.pid")
    check(os.path.exists(pid_file), "Gateway daemon running" if os.path.exists(pid_file)
          else "Gateway daemon not running", "" if os.path.exists(pid_file) else "hermes gateway status")
    check(os.path.exists(_SKILL), "rex skill registered (rex:rex)" if os.path.exists(_SKILL)
          else "rex skill file missing", "" if os.path.exists(_SKILL) else "reinstall the plugin")

    print("\n◆ Rex integration")
    for ok, text, fix in rows:
        print(f"  {'✓' if ok else '✗'} {text}")
        if fix and not ok:
            for line in fix.splitlines():
                print(f"    → {line}")
    if fixed:
        print(f"    → {fixed}")
        print("    → start a new Hermes session for it to take effect")
    hard = [r for r in rows if not r[0] and "optional" not in r[2]]
    print("\n  All good." if not rows or not hard else "\n  Fix the ✗ items above.")
    return 0 if not hard else 1


# --- registration -------------------------------------------------------------

def register(ctx) -> None:
    for key in _settings:
        try:
            value = ctx.get_config(key, None)
            if value is not None:
                _settings[key] = value
        except Exception:
            pass
    for name, cb in _HOOKS:
        ctx.register_hook(name, cb)
    for what, call in (
        ("prompt section", lambda: ctx.register_system_prompt_section("rex", rex_context, max_chars=4000)),
        ("skill", lambda: ctx.register_skill("rex", _SKILL, description=(
            "Use the rex CLI to manage Rex sessions, open tabs, arrange panes, run commands, "
            "inspect terminal output, and control connected Rex apps."))),
        ("cli command", lambda: ctx.register_cli_command(
            "rex", "Rex terminal integration (doctor)", _doctor_setup, _doctor,
            description="Checks the Hermes x Rex wiring and can add the optional config line.")),
    ):
        try:
            call()
        except Exception as exc:  # an older Hermes without one of these keeps the rest
            _debug(f"could not register {what}: {exc!r}")
