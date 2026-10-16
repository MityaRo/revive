# revive

Saves the iTerm2 hotkey window and brings it back after a reboot or an iTerm2 update: tab order and names, split panes, each pane's font size and background colour, its working directory, and the Claude Code session that was running in it.

Needs macOS, iTerm2 with its Python API enabled, and Python 3.11 or newer. Claude Code is optional: without it revive restores the layout and working directories only.

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/revive" ~/.local/bin/revive
revive --install
```

`--install` adds a `SessionStart`/`SessionEnd` hook to `~/.claude/settings.json`, which records which Claude session (and process) runs in which iTerm2 pane, and a launchd agent that writes `auto-<date>.json` every 300 seconds, keeping the last 20. Autosaves never overwrite `latest.json`: after a reboot the first autosave would otherwise replace it with the single empty tab the hotkey window opens with. `revive --uninstall` removes both again. The hook and the agent point at this checkout, so re-run `--install` if you move the folder. iTerm2 needs Settings > General > Magic > "Enable Python API" turned on.

## Use

```sh
revive -s                   # --save: snapshot the hotkey window to ~/.revive/snapshots/latest.json
revive -s before-update     # a named snapshot
revive -i                   # --inspect: what a snapshot holds, with guessed sessions flagged
revive -l                   # --list: every snapshot, including autosaves
revive -r -n                # --restore --dry-run: print each pane's command, change nothing
revive -r                   # rebuild tabs and panes in the open hotkey window, resume each Claude session
revive -r -c                # --close-existing: afterwards close the tabs the window already had
revive -r --no-resume       # layout and directories only
```

Before a reboot: `revive -s`, restart, open iTerm2 and press the hotkey once so the window exists, then run `revive -r -c` from a tab in it. The tab you run it from is closed last.

Restore appends tabs to the open hotkey window; saved regular windows (from `--all-windows`) each get a new window. Each pane's command is sent once its shell is up, with iTerm2 broadcasting suppressed. Before launching anything it refuses when the snapshot gives one session to two panes, or when a wanted session is still running in an iTerm2 pane, because two processes writing one transcript corrupt it; `--force` overrides the second check. Only `--dangerously-skip-permissions`, `--model` and `--permission-mode` are replayed from the original command line. `-d SEC` sets the wait between tabs; `-w` builds into a new window instead of the hotkey window.

## How a pane is matched to a Claude session

In order of reliability, shown as `[source]` in `revive -i`:

1. `hook`: the record the Claude Code hook wrote for that pane's iTerm2 session id, accepted only if its process id is the Claude process on that tty (a `claude -p` run from inside a pane inherits the pane's iTerm2 id and would otherwise hijack it). Exact, but only for sessions started after `--install`.
2. `argv`: the pane's Claude process was started with `--resume <id>` (and not `--fork-session`).
3. `title`: the pane's title (which Claude sets from the conversation's title) matches exactly one transcript in the project directory.
4. `transcript`: the transcript whose first message came soonest after the process started, in the same directory.
5. `transcript-newest`: the most recently written transcript in that directory.

4 and 5 are guesses and are reported on stderr by `-s`, `-i` and `-r`. An id that two panes both match exactly is trusted for neither; a guess never takes an id an exact match holds.

Sessions started by agent teams (`--agent-id`) are rebuilt as plain shells, since only their parent can start them.

## Limits

- Pane sizes are not recorded: splits come back equal.
- "sent commands" means the text reached the pane; whether `claude` started is not checked.
- Only the current layout is restored; scrollback is iTerm2's job (Settings > General > Startup > "Use System Window Restoration Setting", and turn off "Close windows when quitting an application" in macOS Desktop & Dock settings).
- Snapshots live in `~/.revive/snapshots`; autosave keeps the last 20 as `auto-*.json`.

## License

MIT.
