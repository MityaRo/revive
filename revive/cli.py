"""revive: save the iTerm2 hotkey window with its Claude Code sessions, and bring it back after a reboot."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import GUESS_SOURCES, HOME, SNAPSHOTS, claude, panes, write_json

SETTINGS = Path.home() / ".claude" / "settings.json"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.revive.autosave.plist"
AUTOSAVE_KEEP = 20

EXAMPLES = """\
examples:
  revive -s                save the hotkey window as "latest"
  revive -s before-update  save under a name
  revive -i                see what the snapshot holds
  revive -r -n             print what restore would run, change nothing
  revive -r -c             after a reboot: rebuild everything, then close the window's stale tabs
  revive --install         install the Claude Code hook and a 5-minute autosave
"""


def _path(name: str) -> Path:
    return Path(name) if name.endswith(".json") else SNAPSHOTS / f"{name}.json"


def _run(coro_fn):
    """Run an iTerm2 API coroutine and hand back its result. iterm2 is imported here so the hook stays light."""
    import iterm2

    box: dict = {}

    async def main(connection) -> None:
        box["result"] = await coro_fn(connection)

    try:
        iterm2.run_until_complete(main)
    except Exception as e:  # iterm2 raises a plain Exception when the API socket is missing
        sys.exit(f"could not reach iTerm2's API ({e}). Is iTerm2 running with Settings > General > Magic > "
                 "'Enable Python API' on?")
    return box["result"]


def _describe(p: dict) -> str:
    c = p["claude"]
    if c:
        return f"claude --resume {c['session_id'][:8]}… [{c['source']}]"
    return p["job"] or "shell"


def _report_uncertain(snap: dict) -> None:
    """Every pane whose session is a guess or unknown, on stderr."""
    for p in panes(snap):
        where = f"{p['tty']} in {p['cwd']}"
        if p["job"] == "claude" and not p["claude"]:
            print(f"  ! Claude runs in {where} but no session id was found; it comes back as a plain shell",
                  file=sys.stderr)
        elif p["claude"] and p["claude"]["source"] in GUESS_SOURCES:
            print(f"  ? {where}: {_describe(p)} is a guess from transcript timing", file=sys.stderr)


def save(name: str, all_windows: bool, autosave: bool) -> None:
    from . import iterm

    snap = _run(lambda c: iterm.snapshot(c, hotkey_only=not all_windows))
    if not snap["windows"]:
        sys.exit("nothing saved: no hotkey window is open (pass --all-windows to save regular windows)")
    if autosave:
        # Autosaves never touch the snapshot a manual save made: after a reboot the first autosave would
        # otherwise overwrite it with the single empty tab the hotkey window opens with.
        path = SNAPSHOTS / f"auto-{datetime.now().strftime('%Y%m%d-%H%M')}.json"
        for old in sorted(SNAPSHOTS.glob("auto-*.json"))[:-(AUTOSAVE_KEEP - 1)]:  # room for this one
            old.unlink()
    else:
        path = _path(name)
    write_json(path, snap)
    all_panes = panes(snap)
    print(f"saved {path}: {sum(len(w['tabs']) for w in snap['windows'])} tabs, {len(all_panes)} panes, "
          f"{sum(1 for p in all_panes if p['claude'])} Claude sessions resumable")
    _report_uncertain(snap)


def inspect(name: str) -> None:
    snap = _load(name)
    print(f"saved {snap['saved_at']}")
    for w in snap["windows"]:
        print("hotkey window" if w["hotkey"] else "window")
        for i, t in enumerate(w["tabs"], 1):
            print(f"  tab {i}: {t.get('title') or '(no name)'}")
            for p in panes({"windows": [{"tabs": [t]}]}):
                print(f"    {p['font']:<28} {p['cwd'] or '?'}  →  {_describe(p)}")
    _report_uncertain(snap)


def list_snapshots() -> None:
    files = sorted(SNAPSHOTS.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        sys.exit(f"no snapshots in {SNAPSHOTS}. Run `revive -s` first.")
    for f in files:
        snap = json.loads(f.read_text())
        ps = panes(snap)
        print(f"{f.stem:<22} {snap['saved_at']}  {sum(len(w['tabs']) for w in snap['windows']):>2} tabs "
              f"{len(ps):>2} panes {sum(1 for p in ps if p['claude']):>2} claude")


def _load(name: str) -> dict:
    path = _path(name)
    if not path.exists():
        sys.exit(f"no snapshot at {path}. Run `revive -s` first, or `revive -l` to list the ones you have.")
    snap = json.loads(path.read_text())
    if snap.get("version") != 1:
        sys.exit(f"{path} has snapshot version {snap.get('version')!r}; this build reads version 1")
    return snap


def restore(name: str, *, dry_run: bool, resume: bool, close_existing: bool, new_window: bool, force: bool,
            delay: float) -> None:
    from . import iterm

    snap = _load(name)
    launch = resume
    wanted = Counter(p["claude"]["session_id"] for p in panes(snap) if p["claude"])
    dupes = [sid for sid, n in wanted.items() if n > 1]
    if dupes and launch:
        sys.exit(f"snapshot assigns one session to several panes ({', '.join(d[:8] for d in dupes)}); "
                 "resuming it twice would corrupt its transcript. Re-run `revive -s`, or use --no-resume.")
    if dry_run:
        for w in snap["windows"]:
            for t in w["tabs"]:
                print(f"tab {t.get('title') or '(no name)'}")
                for p in panes({"windows": [{"tabs": [t]}]}):
                    print(f"  {iterm.launch_command(p, launch) or '(empty shell)'}")
        _report_uncertain(snap)
        return
    if launch and not force:
        live_snap = _run(lambda c: iterm.snapshot(c, hotkey_only=False))
        live = {p["claude"]["session_id"] for p in panes(live_snap) if p["claude"]} & set(wanted)
        if live:
            sys.exit(f"{len(live)} of these Claude sessions are still running in iTerm2 (resuming them twice "
                     "would corrupt their transcripts). Pass --force if you are sure, or --no-resume to rebuild "
                     "the layout only.")
    _report_uncertain(snap)

    async def go(connection) -> None:
        profiles = await iterm.profile_names(connection)
        await iterm.restore(connection, snap, launch=launch, replace=close_existing, new_window=new_window,
                            profiles=profiles, pause=delay)

    _run(go)


def hook() -> None:
    claude.record_from_hook(json.load(sys.stdin), dict(os.environ))


def _binary() -> Path:
    return Path(sys.argv[0]).resolve()


def install(every: int) -> None:
    binary = _binary()
    _install_hook(binary)
    log = HOME / "autosave.log"
    HOME.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCH_AGENT.stem,
        "ProgramArguments": [str(binary), "--save", "--autosave"],
        "StartInterval": every,
        "RunAtLoad": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)], capture_output=True)
    LAUNCH_AGENT.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "load", str(LAUNCH_AGENT)], check=True)
    print(f"autosaving every {every}s to {SNAPSHOTS}/auto-*.json (keeps {AUTOSAVE_KEEP}; log: {log})")


def _hook_entries(settings: dict, binary: Path) -> list[tuple[list, dict]]:
    """(entry list, entry) pairs for every hook entry in settings that runs this binary."""
    found = []
    for event in ("SessionStart", "SessionEnd"):
        for entry in settings.get("hooks", {}).get(event, []):
            if any(str(binary) in h.get("command", "") for h in entry.get("hooks", [])):
                found.append((settings["hooks"][event], entry))
    return found


def _install_hook(binary: Path) -> None:
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = settings.setdefault("hooks", {})
    command = f"{shlex.quote(str(binary))} --hook"
    changed = False
    for event in ("SessionStart", "SessionEnd"):
        entries = hooks.setdefault(event, [])
        ours = [h for e in entries for h in e.get("hooks", []) if str(binary) in h.get("command", "")]
        if ours and all(h["command"] == command for h in ours):
            continue
        for h in ours:
            h["command"] = command
        if not ours:
            entries.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})
        changed = True
    if changed:
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"installed SessionStart/SessionEnd hooks in {SETTINGS}; Claude sessions started from now on are "
              "matched exactly")
    else:
        print("hooks already installed")


def uninstall() -> None:
    if SETTINGS.exists():
        settings = json.loads(SETTINGS.read_text())
        found = _hook_entries(settings, _binary())
        for entries, entry in found:
            entries.remove(entry)
        if found:
            SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
        print(f"removed {len(found)} hook entries from {SETTINGS}")
    if LAUNCH_AGENT.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT)], capture_output=True)
        LAUNCH_AGENT.unlink()
        print("removed the autosave launch agent")
    else:
        print("no autosave launch agent was installed")


def main() -> None:
    ap = argparse.ArgumentParser(prog="revive", description=__doc__, epilog=EXAMPLES,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    act = ap.add_argument_group("actions (pick one)").add_mutually_exclusive_group()
    act.add_argument("-s", "--save", nargs="?", const="latest", metavar="NAME",
                     help="snapshot the hotkey window (default name: latest)")
    act.add_argument("-r", "--restore", nargs="?", const="latest", metavar="NAME",
                     help="rebuild tabs, panes and Claude sessions from a snapshot")
    act.add_argument("-i", "--inspect", nargs="?", const="latest", metavar="NAME",
                     help="print what a snapshot holds")
    act.add_argument("-l", "--list", action="store_true", help="list saved snapshots")
    act.add_argument("--install", nargs="?", const=300, type=int, metavar="SECONDS",
                     help="install the Claude Code hook and autosave every SECONDS (default 300)")
    act.add_argument("--uninstall", action="store_true", help="remove the hook and the autosave agent")
    act.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)

    opt = ap.add_argument_group("options")
    opt.add_argument("-a", "--all-windows", action="store_true", help="with --save: include regular windows too")
    opt.add_argument("-n", "--dry-run", action="store_true",
                     help="with --restore: print each pane's command and change nothing")
    opt.add_argument("--no-resume", dest="resume", action="store_false",
                     help="with --restore: recreate tabs, panes and directories but do not start claude")
    opt.add_argument("-c", "--close-existing", action="store_true",
                     help="with --restore: afterwards close the tabs the window already had, "
                          "such as the dead shells left after a reboot")
    opt.add_argument("-w", "--new-window", action="store_true",
                     help="with --restore: build into a fresh window instead of the hotkey window")
    opt.add_argument("-f", "--force", action="store_true",
                     help="with --restore: resume even if a session is still running somewhere")
    opt.add_argument("-d", "--delay", type=float, default=0.4, metavar="SEC",
                     help="with --restore: seconds to wait between tabs (default 0.4)")
    opt.add_argument("--autosave", action="store_true", help=argparse.SUPPRESS)  # set by the launchd agent

    a = ap.parse_args()
    if a.save is not None:
        save(a.save, a.all_windows, a.autosave)
    elif a.restore is not None:
        restore(a.restore, dry_run=a.dry_run, resume=a.resume, close_existing=a.close_existing,
                new_window=a.new_window, force=a.force, delay=a.delay)
    elif a.inspect is not None:
        inspect(a.inspect)
    elif a.list:
        list_snapshots()
    elif a.install is not None:
        install(a.install)
    elif a.uninstall:
        uninstall()
    elif a.hook:
        hook()
    else:
        ap.print_help()
