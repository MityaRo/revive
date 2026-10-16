"""Find the Claude Code session running in a terminal pane."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from . import SESSION_MAP, write_json

PROJECTS = Path.home() / ".claude" / "projects"
PS_LINE = re.compile(r"^\s*(\d+)\s+(\w{3} \w{3} [ \d]\d \d\d:\d\d:\d\d \d{4})\s+(.*)$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TITLE_LINE = re.compile(r'^([^:]+\.jsonl):"type":"(?:ai|custom)-title","(?:aiTitle|customTitle)":"((?:[^"\\]|\\.)*)"')
RESUME_FLAGS = {"--resume", "-r"}
# The only launch options restore replays. Anything else (prompts, forks, one-shot flags) stays behind.
REPLAY_FLAGS = {"--dangerously-skip-permissions"}
REPLAY_FLAGS_WITH_VALUE = {"--model", "--permission-mode"}


@dataclass
class ClaudeProcess:
    pid: int
    started: datetime
    argv: list[str]
    cwd: str | None

    @property
    def is_agent(self) -> bool:
        return "--agent-id" in self.argv

    @property
    def flags(self) -> list[str]:
        out: list[str] = []
        args = self.argv[1:]
        for i, a in enumerate(args):
            if a in REPLAY_FLAGS:
                out.append(a)
            elif a in REPLAY_FLAGS_WITH_VALUE and i + 1 < len(args):
                out += [a, args[i + 1]]
        return out


@dataclass
class ClaudeSession:
    session_id: str
    source: str  # hook | argv | title | transcript | transcript-newest
    flags: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _is_claude(binary: str) -> bool:
    return binary == "claude" or binary.endswith("/claude") or "/.local/share/claude/versions/" in binary


def find_process(tty: str) -> ClaudeProcess | None:
    """The Claude Code process on this tty, if any (``tty`` is like /dev/ttys020)."""
    out = subprocess.run(["ps", "-t", tty.removeprefix("/dev/"), "-o", "pid=", "-o", "lstart=", "-o", "command="],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = PS_LINE.match(line)
        if not m:
            continue
        argv = m.group(3).split()
        if argv and _is_claude(argv[0]):
            pid = int(m.group(1))
            started = datetime.strptime(m.group(2), "%a %b %d %H:%M:%S %Y").astimezone()
            return ClaudeProcess(pid, started, argv, _cwd(pid))
    return None


def _cwd(pid: int) -> str | None:
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def resolve_strong(proc: ClaudeProcess, iterm_session_id: str, pane_title: str | None) -> ClaudeSession | None:
    """Exact sources: the hook record for this very process, an explicit --resume flag, or a unique transcript
    carrying the pane's title."""
    if proc.is_agent:
        return None  # team-member agents are spawned by their parent and cannot be resumed on their own
    sid = _from_hook(iterm_session_id, proc)
    if sid:
        return ClaudeSession(sid, "hook", proc.flags)
    sid = _from_argv(proc.argv)
    if sid:
        return ClaudeSession(sid, "argv", proc.flags)
    sid = _from_title(proc, pane_title)
    if sid:
        return ClaudeSession(sid, "title", proc.flags)
    return None


def resolve_weak(proc: ClaudeProcess, claimed: set[str]) -> ClaudeSession | None:
    """Guesses from transcript timing, skipping ids already claimed by other panes."""
    if proc.is_agent or not proc.cwd:
        return None
    candidates = [p for p in project_dir(proc.cwd).glob("*.jsonl") if UUID.match(p.stem) and p.stem not in claimed]
    if not candidates:
        return None
    # The first transcript line is the first message, which can come long after launch, so look for the
    # transcript that started soonest after the process did.
    best: tuple[float, Path] | None = None
    for p in candidates:
        ts = _first_timestamp(p)
        if ts is None:
            continue
        gap = (ts - proc.started).total_seconds()
        if gap >= -5 and (best is None or gap < best[0]):
            best = (gap, p)
    if best:
        return ClaudeSession(best[1].stem, "transcript", proc.flags)
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return ClaudeSession(newest.stem, "transcript-newest", proc.flags)


def _from_hook(iterm_session_id: str, proc: ClaudeProcess) -> str | None:
    path = SESSION_MAP / f"{iterm_session_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # A record counts only if it was written by this very process: a Claude started from inside the pane
    # (say, a headless `claude -p` run by a tool) inherits the pane's iTerm id and would otherwise hijack it.
    if data.get("pid") != proc.pid:
        return None
    return data.get("session_id")


def _from_argv(argv: list[str]) -> str | None:
    if "--fork-session" in argv:
        return None  # the resumed id was only the starting point; the live conversation has a new one
    for i, a in enumerate(argv[:-1]):
        if a in RESUME_FLAGS and UUID.match(argv[i + 1]):
            return argv[i + 1]
    return None


def project_dir(cwd: str) -> Path:
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _first_timestamp(transcript: Path) -> datetime | None:
    with transcript.open() as f:
        for _ in range(20):
            line = f.readline()
            if not line:
                break
            m = re.search(r'"timestamp":"([^"]+)"', line)
            if m:
                return datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    return None


def clean_title(name: str | None) -> str | None:
    """iTerm shows Claude's title as e.g. '✳ Calorie counting (node)'; keep just the words."""
    if not name:
        return None
    name = re.sub(r"\s*\(\w+\)$", "", name)
    return re.sub(r"^[^A-Za-z0-9]+", "", name).strip() or None


_titles_cache: dict[Path, dict[str, str]] = {}


def _transcript_titles(project: Path) -> dict[str, str]:
    """Latest ai/custom title per transcript stem, found with one grep over the project directory."""
    if project not in _titles_cache:
        titles: dict[str, str] = {}
        files = sorted(project.glob("*.jsonl")) if project.is_dir() else []
        if files:
            out = subprocess.run(["grep", "-H", "-o", "-E",
                                  r'"type":"(ai|custom)-title","(aiTitle|customTitle)":"([^"\\]|\\.)*"', *files],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
            for line in out.splitlines():
                m = TITLE_LINE.match(line)
                if m:
                    titles[Path(m.group(1)).stem] = json.loads(f'"{m.group(2)}"')
        _titles_cache[project] = titles
    return _titles_cache[project]


def _from_title(proc: ClaudeProcess, pane_title: str | None) -> str | None:
    title = clean_title(pane_title)
    if not title or not proc.cwd:
        return None
    matches = [stem for stem, t in _transcript_titles(project_dir(proc.cwd)).items()
               if t.casefold() == title.casefold() and UUID.match(stem)]
    return matches[0] if len(matches) == 1 else None


def record_from_hook(payload: dict, env: dict[str, str]) -> None:
    """Called by the Claude Code SessionStart/SessionEnd hook with the hook's stdin payload and environment."""
    iterm_session_id = env.get("ITERM_SESSION_ID")
    if not iterm_session_id:
        return
    path = SESSION_MAP / f"{iterm_session_id.split(':')[-1]}.json"
    pid = int(env["CLAUDE_PID"]) if env.get("CLAUDE_PID", "").isdigit() else None
    if payload.get("hook_event_name") == "SessionEnd":
        if path.exists():
            current = json.loads(path.read_text())
            if current.get("session_id") == payload.get("session_id") and current.get("pid") == pid:
                path.unlink()
        return
    write_json(path, {
        "session_id": payload.get("session_id"),
        "pid": pid,
        "cwd": payload.get("cwd"),
        "source": payload.get("source"),
        "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
