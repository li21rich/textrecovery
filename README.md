# textrecovery

An install-and-forget-about-it background tool that keeps a rolling copy of what you've typed recently, so a paragraph you accidentally deleted isn't gone forever. Once you actually need it, open it and find what you've recently typed. Keystrokes only, so no clipboard or screenshots. 

## Install

```
python3 textrecovery.py --install
```

This starts the recorder at login. Remove with `--uninstall`.

- **macOS:** grant Accessibility permission when prompted
- **Linux:** use an X11 session (Wayland is unsupported)
- **Windows:** The installer adds `textrecovery` to your PATH (open a new terminal afterwards)

## Use

```
textrecovery --gui                # open textrecovery
textrecovery --blocks 3           # last 3 typed blocks
textrecovery --blocks 3 --copy    # ...straight to the clipboard
textrecovery --grep "invoice"     # search everything buffered
textrecovery --last 2000          # raw last 2000 characters
textrecovery --status             # buffer size, and whether the recorder is running
textrecovery --clear              # wipe it
```

In the viewer, pick a block on the left to preview it. **Copy block** (or double-clicking the block) copies that one; **Copy all** copies every block currently listed (respecting the filter), oldest first. You can always select text and use the usual copy shortcut (e.g. Ctrl + C). The list refreshes every couple of seconds.

## What gets recorded

Letters, numbers, punctuation, spaces, enter, and tab indents (as 4 spaces). Backspace, delete, and arrow keys are ignored. Pasted text and input-method text (CJK IME, emoji picker) aren't captured.

## Settings

`~/.textrecovery/config.json`, or env vars like `TEXT_RECOVERY_MAX_BYTES=524288`.
Run `textrecovery --config` to print the file's path.

| key | default | meaning |
| --- | --- | --- |
| `dir` | `~/.textrecovery` | storage location |
| `max_age_hours` | `72` | blocks older than this are dropped |
| `max_bytes` | `262144` | hard size cap (backstop for heavy days) |
| `flush_secs` | `5.0` | max seconds between writes (also writes after a 2s pause) |
| `idle_marker_secs` | `90` | pause that starts a new block |
| `alnum_only` | `false` | drop spaces and punctuation |
| `markers` | `true` | timestamp blocks |

## Caveats

Everything typed in any app is captured, **passwords and games included**, as
plain text in `store.txt`. Point `dir` at an encrypted volume if that matters.
Nothing is ever sent anywhere. This should not be used as a keylogger.