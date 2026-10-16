"""Capture and rebuild iTerm2 window layouts through the Python API."""
from __future__ import annotations

import asyncio
import os
import shlex
from datetime import datetime, timezone
from typing import Any

import iterm2

from . import claude, leaves, panes

Node = dict[str, Any]
SHELLS = {"zsh", "bash", "fish", "sh"}


async def _var(obj: Any, name: str) -> Any:
    try:
        return await obj.async_get_variable(name)
    except Exception:
        return None


# --- save -------------------------------------------------------------------

async def snapshot(connection: iterm2.Connection, hotkey_only: bool) -> dict:
    app = await iterm2.async_get_app(connection)
    windows = []
    for w in app.terminal_windows:
        hotkey = bool(await _var(w, "isHotkeyWindow"))
        if hotkey_only and not hotkey:
            continue
        tabs = [await _snapshot_tab(t) for t in w.tabs]
        windows.append({"hotkey": hotkey, "tabs": tabs})
    snap = {"version": 1, "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "windows": windows}
    _resolve_sessions(snap)
    return snap


async def _snapshot_tab(tab: iterm2.Tab) -> dict:
    return {"title": await _var(tab, "titleOverride") or None, "root": await _snapshot_node(tab.root)}


async def _snapshot_node(node: Any) -> Node:
    if isinstance(node, iterm2.Splitter):
        children = [await _snapshot_node(c) for c in node.children]
        if len(children) == 1:
            return children[0]
        return {"split": "vertical" if node.vertical else "horizontal", "children": children}
    return await _snapshot_pane(node)


async def _snapshot_pane(s: iterm2.Session) -> Node:
    profile = await s.async_get_profile()
    bg = profile.background_color
    tty = await _var(s, "tty")
    pane: Node = {
        "iterm_session_id": s.session_id,
        "profile": profile.name,
        "font": profile.normal_font,
        "bg": [bg.red, bg.green, bg.blue, bg.alpha] if bg else None,
        "cwd": await _var(s, "path"),
        "tty": tty,
        "title": await _var(s, "autoName"),
        "job": await _var(s, "jobName"),
        "claude": None,
    }
    proc = claude.find_process(tty) if tty else None
    if proc:
        pane["cwd"] = proc.cwd or pane["cwd"]
        pane["job"] = "claude-agent" if proc.is_agent else "claude"
        pane["_proc"] = proc
    return pane


def _resolve_sessions(snap: dict) -> None:
    """Exact matches first; an id two panes both claim exactly is trusted for neither, and guesses never
    take an id an exact match holds."""
    with_proc = [p for p in panes(snap) if "_proc" in p]
    strong: dict[str, list[Node]] = {}
    for p in with_proc:
        found = claude.resolve_strong(p["_proc"], p["iterm_session_id"], p["title"])
        if found:
            p["claude"] = found.to_dict()
            strong.setdefault(found.session_id, []).append(p)
    for sid, owners in strong.items():
        if len(owners) > 1:
            for p in owners:
                p["claude"] = None
    claimed = {sid for sid, owners in strong.items() if len(owners) == 1}
    for p in with_proc:
        if p["claude"] is None:
            found = claude.resolve_weak(p["_proc"], claimed)
            if found:
                p["claude"] = found.to_dict()
                claimed.add(found.session_id)
        del p["_proc"]


# --- restore ----------------------------------------------------------------

def _customizations(pane: Node) -> iterm2.LocalWriteOnlyProfile:
    p = iterm2.LocalWriteOnlyProfile()
    if pane.get("font"):
        p.set_normal_font(pane["font"])
    if pane.get("bg"):
        r, g, b, a = pane["bg"]
        p.set_background_color(iterm2.Color(r, g, b, a))
    return p


def _first_pane(node: Node) -> Node:
    return node if "children" not in node else _first_pane(node["children"][0])


def launch_command(pane: Node, launch: bool) -> str | None:
    parts = []
    if pane.get("cwd"):
        parts.append(f"cd {shlex.quote(pane['cwd'])}")
    c = pane.get("claude")
    if launch and c and c.get("session_id"):
        parts.append(shlex.join(["claude", *c.get("flags", []), "--resume", c["session_id"]]))
    return " && ".join(parts) or None


async def restore(connection: iterm2.Connection, snap: dict, *, launch: bool, replace: bool,
                  new_window: bool, profiles: set[str], pause: float, log=print) -> None:
    """Rebuild every window in the snapshot, reporting through ``log`` as it goes.

    The hotkey window's tabs go into the open hotkey window; every other saved window gets a fresh window.
    """
    app = await iterm2.async_get_app(connection)
    me = os.environ.get("ITERM_SESSION_ID", "").split(":")[-1]  # the pane this command runs in, if any
    missing: set[str] = set()
    to_close: list[iterm2.Tab] = []
    tabs_made = sent = 0

    def profile_for(pane: Node) -> str | None:
        name = pane["profile"]
        if name in profiles:
            return name
        if name not in missing:
            missing.add(name)
            log(f"profile {name!r} no longer exists, using the default profile instead")
        return None

    for win in snap["windows"]:
        target = None
        if win["hotkey"] and not new_window:
            target = await _hotkey_window(app)
            if target is None:
                log("no hotkey window is open, so its tabs went into a new regular window")
            else:
                to_close += list(target.tabs)
        for tab in win["tabs"]:
            first = _first_pane(tab["root"])
            if target is None:
                target = await iterm2.Window.async_create(connection, profile=profile_for(first),
                                                          profile_customizations=_customizations(first))
                new_tab = target.tabs[0]
            else:
                new_tab = await target.async_create_tab(profile=profile_for(first),
                                                        profile_customizations=_customizations(first))
            tabs_made += 1
            if tab.get("title"):
                await new_tab.async_set_title(tab["title"])
            sent += await _build(new_tab.current_session, tab["root"], profile_for, launch)
            await asyncio.sleep(pause)
    log(f"created {tabs_made} tab(s) and sent commands to {sent} pane(s)")
    if replace and to_close:
        # Closing the tab this command runs in kills the process, so that tab goes last.
        to_close.sort(key=lambda t: any(s.session_id == me for s in t.sessions))
        log(f"closing {len(to_close)} pre-existing tab(s)")
        for t in to_close:
            await t.async_close(force=True)


async def _hotkey_window(app: iterm2.App) -> iterm2.Window | None:
    for w in app.terminal_windows:
        if await _var(w, "isHotkeyWindow"):
            return w
    return None


async def _build(session: iterm2.Session, node: Node, profile_for, launch: bool) -> int:
    """Split ``session`` into the saved tree and start each pane's command. Returns panes given a command."""
    if "children" not in node:
        await session.async_set_profile_properties(_customizations(node))
        cmd = launch_command(node, launch)
        if not cmd:
            return 0
        await _wait_for_shell(session)
        await session.async_send_text(cmd + "\n", suppress_broadcast=True)
        return 1
    vertical = node["split"] == "vertical"
    sessions = [session]
    for child in node["children"][1:]:
        first = _first_pane(child)
        sessions.append(await sessions[-1].async_split_pane(vertical=vertical, profile=profile_for(first),
                                                            profile_customizations=_customizations(first)))
    return sum([await _build(s, child, profile_for, launch) for s, child in zip(sessions, node["children"])])


async def _wait_for_shell(session: iterm2.Session, timeout: float = 5.0) -> None:
    """Hold the command until the pane's foreground job is a shell, so startup files cannot swallow it."""
    for _ in range(int(timeout / 0.1)):
        job = (await _var(session, "jobName") or "").lstrip("-")
        if job in SHELLS:
            return
        await asyncio.sleep(0.1)


async def profile_names(connection: iterm2.Connection) -> set[str]:
    return {p.name for p in await iterm2.PartialProfile.async_query(connection)}
