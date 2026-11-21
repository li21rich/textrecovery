# textrecovery
Ever type a whole page of text and then accidentally close out of it, losing it forever? It may not happen often, but when it does, it's tragically annoying.

textrecovery is a very simple, set-it-and-forget-it background tool that keeps a rolling copy of what you've typed recently. If you accidentally close a tab, crash an app, or delete a paragraph, you can always get it back.

Once you install it, you won't need to start it manually. It automatically stays in the background upon booting up your computer. It's a single Python file with zero network calls (besides downloading pynput for you on install).

If you ever need to use it, open textrecovery or use the CLI to quickly find what you've recently typed.

<img width="533" height="440" alt="image" src="https://github.com/user-attachments/assets/01ca70c4-11ea-4fb5-92ef-bfcf565cafc5" />

## Install
```
git clone https://github.com/li21rich/textrecovery.git
cd textrecovery
python3 textrecovery.py --install
```

This starts the recorder at login. You can fully remove it with `--uninstall`.

- **macOS:** grant accessibility permission if prompted
- **Linux:** use an X11 session
- **Windows:** The installer adds `textrecovery` to your PATH

## Use
Typing is grouped into timestamped blocks. Old blocks are automatically dropped after 72 hours (or 256 KB).

```
textrecovery --gui                # open the viewer
textrecovery --blocks 3           # print the last 3 blocks
textrecovery --blocks 3 --copy    # copy the last 3 blocks to clipboard
textrecovery --grep "invoice"     # search buffered text
textrecovery --last 2000          # raw last 2000 characters
textrecovery --status             # check buffer size and if recorder is running
textrecovery --clear              # wipe the buffer
```

In the GUI, click a block on the left to preview it. Double-click (or hit **Copy block**) to copy it, or **Copy all** to copy every block matching your current filter. The list refreshes every couple of seconds. You can also always use Ctrl + C.

## Privacy!!
What Gets Recorded:
- **Recorded:** Letters, numbers, punctuation, spaces, Enter, and Tab (as 4 spaces).
- **Ignored:** Backspace, Delete, arrow keys, copy/paste, CJK IMEs, emoji pickers, and screenshots.

Everything typed in any app—including sensitive information—is saved as plain text in `~/.textrecovery/store.txt`. Nothing is ever sent over the network. If plain-text storage on disk is a concern, point `dir` to an encrypted volume in your config.
Because the entire tool is one file (`textrecovery.py`), you can review all the code in a few minutes before running it. 
Nothing is ever sent anywhere. This should not be used as a keylogger for spyware.

## Settings
Edit `~/.textrecovery/config.json` (run `textrecovery --config` to print the path) or set env vars like `TEXT_RECOVERY_MAX_BYTES=524288`.

| Key | Default | Meaning |
| --- | --- | --- |
| `dir` | `~/.textrecovery` | Storage location |
| `max_age_hours` | `72` | Drop blocks older than this |
| `max_bytes` | `262144` | Hard size cap |
| `flush_secs` | `5.0` | Max seconds between disk writes (also writes after a 2s pause) |
| `idle_marker_secs` | `300` | Pause length that starts a new block |
| `alnum_only` | `false` | Drop spaces and punctuation |
| `markers` | `true` | Add timestamps to blocks |
