#!/usr/bin/env python3
"""
rolling keyboard buffer with timestamped blocks.

Keeps a rolling, age-and-size-capped window of everything typed, split
into timestamped blocks whenever you pause. Old data is automatically deleted.
"""

import argparse
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "2.0.0"
LABEL = "ai.local.textrecovery"

DEFAULTS = {
    "dir": "~/.textrecovery",
    "max_bytes": 262144,
    "flush_secs": 5.0,
    "max_age_hours": 72,
    "idle_marker_secs": 90,
    "alnum_only": False,
    "markers": True,
}

VENDOR = Path(DEFAULTS["dir"]).expanduser() / "vendor"

# Pinned, pure-Python wheels, downloaded by --install on Linux and Windows so that no
# pip, compiler, or sudo is needed. (pip would also pull in `evdev`, which compiles from
# source on Linux and isn't used by the default X11 backend.) macOS uses pip: pynput
# needs native pyobjc there.
WHEELS = {
    "six": (
        "https://files.pythonhosted.org/packages/b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed46c2cc5e/six-1.17.0-py2.py3-none-any.whl",
        "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274"),
    "pynput": (
        "https://files.pythonhosted.org/packages/6d/98/bbeb760852adb27f166ce1617f0e51aabb15f21b1e60ea703f2aed3c78ac/pynput-1.8.2-py2.py3-none-any.whl",
        "8cc38cf13a6ab2749cb375678be8a0fd705d7ce49c8001ff5db4007a723bbef1"),
    "python-xlib": (  # Linux only
        "https://files.pythonhosted.org/packages/fc/b8/ff33610932e0ee81ae7f1269c890f697d56ff74b9f5b2ee5d9b7fa2c5355/python_xlib-0.33-py2.py3-none-any.whl",
        "c3534038d42e0df2f1392a1b30a15a4ff5fdc2b86cfa94f072bf11b10a164398"),
}

MARKER_RE = re.compile(r"(?m)^\[((?:\d{4}-\d\d-\d\d )?\d\d:\d\d)\]$")

CFG = {}
KB = None  # pynput.keyboard, imported lazily so the CLI/GUI work without it
LOCK = threading.Lock()
BUF = []
LAST_KEY = 0.0
LAST_DAY = ""
LAST_TRIM = 0.0
WAKE = threading.Event()


# --------------------------------------------------------------------------
# config and storage
# --------------------------------------------------------------------------

def base():
    return Path(CFG["dir"]).expanduser()


def store():
    return base() / "store.txt"


def load_config():
    d = Path(DEFAULTS["dir"]).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    path = d / "config.json"
    cfg = dict(DEFAULTS)
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text()))
        except (OSError, ValueError):
            pass
    else:
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    for k, default in DEFAULTS.items():
        env = os.environ.get("TEXT_RECOVERY_" + k.upper())
        if env is None:
            continue
        if isinstance(default, bool):
            cfg[k] = env.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            cfg[k] = int(env)
        elif isinstance(default, float):
            cfg[k] = float(env)
        else:
            cfg[k] = env
    return cfg, path


def stamp_time(stamp):
    if len(stamp) > 5:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M"))
    now = time.localtime()
    t = time.strptime(stamp, "%H:%M")
    cand = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                        t.tm_hour, t.tm_min, 0, 0, 0, -1))
    if cand > time.time() + 60:
        cand -= 86400
    return cand


def trim():
    p = store()
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    cut_at = time.time() - CFG["max_age_hours"] * 3600
    keep = data
    for m in MARKER_RE.finditer(data):
        try:
            if stamp_time(m.group(1)) >= cut_at:
                keep = data[m.start():]
                break
        except ValueError:
            continue
    if len(keep) > CFG["max_bytes"]:
        keep = keep[-CFG["max_bytes"] // 2:]
        i = keep.find("\n\n[")
        if i > 0:
            keep = keep[i:]
    if keep == data:
        return
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(keep, encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def maybe_trim():
    global LAST_TRIM
    if time.time() - LAST_TRIM < 60:
        return
    LAST_TRIM = time.time()
    trim()


def read_all():
    try:
        return store().read_text(encoding="utf-8", errors="replace")[-CFG["max_bytes"]:]
    except OSError:
        return ""


def split_blocks(text):
    parts = MARKER_RE.split(text)
    pairs = [(None, parts[0])] if parts else []
    for i in range(1, len(parts) - 1, 2):
        pairs.append((parts[i], parts[i + 1]))
    return [(s, b) for s, b in pairs if b.strip()]


def copy_clip(text):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

def marker():
    global LAST_DAY
    now = time.localtime()
    day = time.strftime("%Y-%m-%d", now)
    hm = time.strftime("%H:%M", now)
    stamp = f"{day} {hm}" if day != LAST_DAY else hm
    LAST_DAY = day
    return f"\n\n[{stamp}]\n"


def ok(c):
    return c.isprintable() and (c.isalnum() or not CFG["alnum_only"])


def on_press(key):
    global LAST_KEY
    now = time.time()
    idle = now - LAST_KEY
    LAST_KEY = now
    if idle > 2.0:
        WAKE.set()
    with LOCK:
        if CFG["markers"] and idle > CFG["idle_marker_secs"] and (BUF or store().exists()):
            BUF.append(marker())
        c = getattr(key, "char", None)
        if isinstance(c, str) and len(c) == 1 and ok(c):
            BUF.append(c)
        elif key is KB.Key.space:
            if not CFG["alnum_only"]:
                BUF.append(" ")
        elif key is KB.Key.enter:
            BUF.append("\n")
        elif key is KB.Key.tab:
            BUF.append("    ")


def flush():
    with LOCK:
        if not BUF:
            return
        data = "".join(BUF)
        BUF.clear()
    try:
        with store().open("a", encoding="utf-8") as f:
            f.write(data)
    except OSError:
        return
    maybe_trim()


def flusher(stop):
    while True:
        WAKE.wait(CFG["flush_secs"])
        WAKE.clear()
        flush()
        if stop.is_set():
            return


LOCK_AT = 4096  # byte we lock, past the pid text, so other processes can still read the pid
LOCKF = None    # held open for the life of the recorder


def _lock(f, take):
    """Take/release the recorder lock. The OS drops it if the process dies or the
    machine reboots, so (unlike a bare PID file) it can never go stale."""
    if os.name == "nt":
        import msvcrt
        f.seek(LOCK_AT)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if take else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, (fcntl.LOCK_EX | fcntl.LOCK_NB) if take else fcntl.LOCK_UN)


def running_pid():
    """PID of the live recorder, or None."""
    try:
        f = open(base() / "pid", "r+b", buffering=0)
    except OSError:
        return None
    try:
        try:
            _lock(f, True)
        except OSError:  # someone else holds it: the recorder is alive
            f.seek(0)
            try:
                return int(f.read(32).split()[0])
            except (ValueError, IndexError):
                return -1
        _lock(f, False)
        return None
    finally:
        f.close()


def wait_running(secs):
    end = time.time() + secs
    while time.time() < end:
        if running_pid():
            return True
        time.sleep(0.25)
    return False


def add_vendor():
    """Make the downloaded copies importable (after anything the system provides)."""
    if VENDOR.is_dir() and str(VENDOR) not in sys.path:
        sys.path.append(str(VENDOR))


def have_pynput():
    import importlib.util
    add_vendor()
    need = ["pynput"] + (["Xlib"] if platform.system() == "Linux" else [])
    return all(importlib.util.find_spec(m) for m in need)


def vendor_install():
    import hashlib
    import io
    import urllib.request
    import zipfile
    names = ["six", "pynput"] + (["python-xlib"] if platform.system() == "Linux" else [])
    VENDOR.mkdir(parents=True, exist_ok=True)
    for n in names:
        url, sha = WHEELS[n]
        data = urllib.request.urlopen(url, timeout=60).read()
        if hashlib.sha256(data).hexdigest() != sha:
            raise ValueError(f"checksum mismatch for {n}; refusing to install it")
        zipfile.ZipFile(io.BytesIO(data)).extractall(VENDOR)


def run():
    global KB
    add_vendor()
    try:
        from pynput import keyboard as KB
    except ImportError:
        print("pynput is missing. Run: python3 textrecovery.py --install", file=sys.stderr)
        return 1

    global LOCKF
    b = base()
    b.mkdir(parents=True, exist_ok=True)
    (b / "pid").touch()
    LOCKF = open(b / "pid", "r+b", buffering=0)
    try:
        _lock(LOCKF, True)
    except OSError:
        print(f"already running (pid {running_pid()})")
        return 0
    LOCKF.seek(0)
    LOCKF.truncate()
    LOCKF.write(str(os.getpid()).encode())

    stop = threading.Event()
    threading.Thread(target=flusher, args=(stop,), daemon=True).start()
    with KB.Listener(on_press=on_press) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            pass
    stop.set()
    WAKE.set()
    flush()
    return 0


# --------------------------------------------------------------------------
# viewer
# --------------------------------------------------------------------------

def gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        print("tkinter is missing (Linux: install python3-tk)", file=sys.stderr)
        return 1

    root = tk.Tk()
    root.title("textrecovery")
    root.geometry("1080x1080")
    blocks, shown = [], []

    def fill():
        sel = selected()
        scroll = lst.yview()[0]
        needle = q.get().lower()
        lst.delete(0, "end")
        shown.clear()
        for stamp, body in blocks:
            if needle and needle not in body.lower():
                continue
            snippet = " ".join(body.split())[:70] or "(blank)"
            lst.insert("end", f"[{stamp or 'start'}]  {snippet}")
            shown.append((stamp, body))
        lst.yview_moveto(scroll)
        keep = next((i for i, (s, _) in enumerate(shown) if sel and s == sel[0]), None)
        if keep is None:
            txt.delete("1.0", "end")
        else:
            lst.selection_set(keep)
            if shown[keep][1] != sel[1]:  # only redraw the preview if its text changed
                preview()

    def load():
        blocks[:] = split_blocks(read_all())[::-1]
        fill()

    def selected():
        sel = lst.curselection()
        return shown[sel[0]] if sel else None

    def preview():
        pick = selected()
        txt.delete("1.0", "end")
        if pick:
            txt.insert("1.0", f"[{pick[0] or 'start'}]\n{pick[1].strip()}")

    def put(data):
        root.clipboard_clear()
        root.clipboard_append(data)
        root.update()

    def copy_one():
        pick = selected()
        if pick:
            put(pick[1].strip())

    def copy_all():
        if shown:
            put("\n\n".join(b.strip() for _, b in reversed(shown)))

    def clear():
        if messagebox.askyesno("textrecovery", "Delete everything buffered?"):
            store().unlink(missing_ok=True)
            load()

    last = [object()]

    def poll():
        """Reload whenever the recorder has written something new."""
        try:
            st = store().stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig != last[0]:
            last[0] = sig
            load()
        root.after(2000, poll)

    top = ttk.Frame(root, padding=8)
    top.pack(fill="x")
    ttk.Label(top, text="Filter").pack(side="left")
    q = ttk.Entry(top)
    q.pack(side="left", fill="x", expand=True, padx=6)
    q.bind("<KeyRelease>", lambda e: fill())
    ttk.Button(top, text="Copy all", command=copy_all).pack(side="left", padx=4)
    body = ttk.PanedWindow(root, orient="horizontal")
    body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    left, right = ttk.Frame(body), ttk.Frame(body)
    lst = tk.Listbox(left, exportselection=False, activestyle="none")
    lst.pack(fill="both", expand=True)
    lst.bind("<<ListboxSelect>>", lambda e: preview())
    lst.bind("<Double-Button-1>", lambda e: copy_one())
    txt = tk.Text(right, wrap="word", undo=False)
    txt.pack(fill="both", expand=True)
    body.add(left, weight=1)
    body.add(right, weight=2)

    bottom = ttk.Frame(root, padding=(8, 0, 8, 8))
    bottom.pack(fill="x")
    ttk.Button(bottom, text="Copy block", command=copy_one).pack(side="left")
    ttk.Button(bottom, text="Clear buffer", command=clear).pack(side="right")

    poll()
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# install / uninstall
# --------------------------------------------------------------------------

def sh(*cmd):
    try:
        return subprocess.run(cmd, check=False, capture_output=True)
    except OSError:
        return None


def locations():
    """(directory, installed script path)"""
    if platform.system() == "Windows":
        d = Path.home() / ".textrecovery"  # not AppData: Store Python sandboxes that
        return d, d / "textrecovery.py"
    d = Path.home() / ".local" / "bin"
    return d, d / "textrecovery"


def artifacts():
    """(autostart definition or None, viewer launcher)"""
    h, s = Path.home(), platform.system()
    if s == "Darwin":
        return (h / "Library/LaunchAgents" / (LABEL + ".plist"),
                h / "Applications/textrecovery-view.command")
    if s == "Linux":
        return (h / ".config/systemd/user/textrecovery.service",
                h / ".local/share/applications/textrecovery-view.desktop")
    return None, desktop() / "textrecovery.lnk"


def desktop():
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(0, 0x10, 0, 0, buf)  # works with OneDrive-redirected Desktops
    return Path(buf.value or Path.home() / "Desktop")


def ps(script):
    """Run PowerShell (encoded, so quoting can't bite us)."""
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    return sh("powershell", "-NoProfile", "-EncodedCommand", enc)


def stop_recorder():
    """Stop a running recorder (Windows; the other OSes restart via their service manager)."""
    pid = running_pid()
    if pid and pid > 0:
        sh("taskkill", "/PID", str(pid), "/F")
    for _ in range(20):
        if not running_pid():
            return
        time.sleep(0.25)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def register_task(pyw, dest):
    """Create the logon task. Returns None on success, else a reason string."""
    esc = lambda p: str(p).replace("'", "''")
    r = ps("$ErrorActionPreference='Stop';"
           f"$a=New-ScheduledTaskAction -Execute '{esc(pyw)}' -Argument '\"{esc(dest)}\"';"
           '$t=New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME";'
           "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
           "-ExecutionTimeLimit (New-TimeSpan -Seconds 0);"
           "Register-ScheduledTask -TaskName textrecovery -Action $a -Trigger $t "
           "-Settings $s -Force | Out-Null;"
           "Get-ScheduledTask -TaskName textrecovery | Out-Null")
    if r is None:
        return "powershell not found"
    if r.returncode:
        return r.stderr.decode(errors="replace").strip().splitlines()[0] if r.stderr else "unknown error"
    return None


def set_run_key(pyw, dest):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, "textrecovery", 0, winreg.REG_SZ, f'"{pyw}" "{dest}"')


def win_path(folder, add):
    """Add or remove a folder in the user's PATH (Windows)."""
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        try:
            cur, typ = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            cur, typ = "", winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(";") if p and p.lower() != str(folder).lower()]
        if add:
            parts.append(str(folder))
        winreg.SetValueEx(k, "Path", 0, typ, ";".join(parts))
    # tell Explorer and new terminals to reload the environment. Non-blocking:
    # SendMessageTimeoutW waits on every window and can stall for seconds.
    ctypes.windll.user32.SendNotifyMessageW(0xFFFF, 0x1A, 0, "Environment")


def install():
    system = platform.system()
    py = sys.executable
    in_venv = sys.prefix != sys.base_prefix

    if not have_pynput():
        if system == "Darwin":
            pip = [py, "-m", "pip", "install", "--quiet", "pynput"] + ([] if in_venv else ["--user"])
            if subprocess.run(pip).returncode:
                print("Could not install pynput. Create a venv, `pip install pynput` in it,\n"
                      "and run --install with that python.", file=sys.stderr)
                return 1
        else:
            try:
                vendor_install()
            except Exception as e:
                print(f"Could not download pynput ({e}).\n"
                      "Check your internet connection, or install it yourself and re-run\n"
                      "(Debian/Ubuntu: sudo apt install python3-pynput).", file=sys.stderr)
                return 1

    bindir, dest = locations()
    svc, launcher = artifacts()
    bindir.mkdir(parents=True, exist_ok=True)
    me = Path(__file__).resolve()
    if me != dest.resolve():
        shutil.copy2(me, dest)
    dest.chmod(0o755)
    launcher.parent.mkdir(parents=True, exist_ok=True)

    if system == "Darwin":
        svc.parent.mkdir(parents=True, exist_ok=True)
        svc.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": [py, str(dest)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardErrorPath": "/tmp/textrecovery.err",
        }))
        domain = f"gui/{os.getuid()}"
        sh("launchctl", "bootout", f"{domain}/{LABEL}")
        sh("launchctl", "bootstrap", domain, str(svc))
        launcher.write_text(f"#!/bin/sh\nexec {shlex.quote(py)} {shlex.quote(str(dest))} --gui\n")
        launcher.chmod(0o755)
        if not wait_running(8):
            print("WARNING: the recorder did not start; see /tmp/textrecovery.err", file=sys.stderr)
        print(f"Installed. Now enable Accessibility for {py}")
        print("in System Settings > Privacy & Security > Accessibility.")
        print("Viewer: double-click ~/Applications/textrecovery-view.command")

    elif system == "Linux":
        svc.parent.mkdir(parents=True, exist_ok=True)
        svc.write_text(
            "[Unit]\nDescription=textrecovery keystroke buffer\n\n"
            f'[Service]\nExecStart="{py}" "{dest}"\nRestart=always\n\n'
            "[Install]\nWantedBy=default.target\n")
        sh("systemctl", "--user", "daemon-reload")
        sh("systemctl", "--user", "enable", "textrecovery")
        sh("systemctl", "--user", "restart", "textrecovery")
        launcher.write_text(
            "[Desktop Entry]\nType=Application\nName=textrecovery\n"
            f'Exec="{py}" "{dest}" --gui\nTerminal=false\nCategories=Utility;\n')
        if subprocess.run([py, "-c", "import tkinter"], capture_output=True).returncode:
            print("Note: --gui needs tkinter (Debian/Ubuntu: sudo apt install python3-tk).")
        if not wait_running(8):
            print("WARNING: the recorder did not start. Check: journalctl --user -u textrecovery -n 20\n"
                  "(it needs an X11 session, with DISPLAY available to user services)", file=sys.stderr)
            return 1
        print("Installed. On Wayland this needs an X11 session to see keystrokes.")
        print("Viewer: launch 'textrecovery' from your app menu, or run: textrecovery --gui")

    elif system == "Windows":
        pyw = Path(py).with_name("pythonw.exe")
        pyw = pyw if pyw.exists() else Path(py)
        stop_recorder()
        # Task Scheduler, not just the registry Run key or Startup folder: packaged
        # (Microsoft Store) Python sandboxes writes to AppData and HKCU\Software, but the
        # Task Scheduler service stores tasks outside that sandbox. We then start the
        # recorder *through the task*, which proves the logon launch really works.
        why = register_task(pyw, dest)
        if not why:
            ps("Start-ScheduledTask -TaskName textrecovery")
            if not wait_running(8):
                why = "the task registered but did not start the recorder"
        if why:
            try:
                set_run_key(pyw, dest)
            except OSError:
                pass
            subprocess.Popen([str(pyw), str(dest)], close_fds=True,
                             creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)
            wait_running(4)
            print(f"WARNING: Task Scheduler autostart did not work ({why}).\n"
                  "Fell back to the registry Run key. Microsoft Store Python sandboxes that key,\n"
                  "so it may not take effect at login. Check after your next login with --status.",
                  file=sys.stderr)
        try:
            (dest.parent / "textrecovery.cmd").write_text(f'@"{py}" "{dest}" %*\r\n')
            win_path(dest.parent, add=True)
        except OSError as e:
            print(f"Could not add the textrecovery command to PATH: {e}", file=sys.stderr)
        esc = lambda p: str(p).replace("'", "''")
        ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{esc(launcher)}');"
           f"$s.TargetPath='{esc(pyw)}';$s.Arguments='\"{esc(dest)}\" --gui';$s.Save()")
        if not running_pid():
            print("The recorder is not running. Try: pythonw " + str(dest), file=sys.stderr)
            return 1
        if not why:
            print("Installed and verified: Task Scheduler started the recorder and will at every login.")
        else:
            print("Installed. The recorder is running now.")
        print("Viewer: shortcut on your Desktop.")
        print("CLI: open a NEW terminal (close all Windows Terminal windows) and run: textrecovery --status")
        print(f'     until then: python "{dest}" --status')

    else:
        print(f"--install doesn't support {system}; run `{py} {me}` at login yourself.")
        return 1

    if system != "Windows" and str(bindir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Note: {bindir} is not on your PATH.")
    return 0


def uninstall():
    system = platform.system()
    _, dest = locations()
    svc, launcher = artifacts()

    if system == "Darwin":
        sh("launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}")
    elif system == "Linux":
        sh("systemctl", "--user", "disable", "--now", "textrecovery")
    elif system == "Windows":
        ps("Unregister-ScheduledTask -TaskName textrecovery -Confirm:$false -ErrorAction SilentlyContinue")
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "textrecovery")
        except OSError:
            pass
        stop_recorder()
        try:
            (dest.parent / "textrecovery.cmd").unlink(missing_ok=True)
            win_path(dest.parent, add=False)
        except OSError:
            pass

    for p in (svc, launcher, dest):
        if p:
            p.unlink(missing_ok=True)
    shutil.rmtree(VENDOR, ignore_errors=True)
    if system == "Linux":
        sh("systemctl", "--user", "daemon-reload")
    print(f"Uninstalled. Your buffer is still in {base()}; run `textrecovery --clear` first to wipe it,")
    print("or delete that folder.")
    return 0


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main():
    global CFG
    ap = argparse.ArgumentParser(prog="textrecovery", description="rolling keystroke buffer")
    ap.add_argument("--gui", action="store_true", help="open the viewer window")
    ap.add_argument("--install", action="store_true", help="install and start at login")
    ap.add_argument("--uninstall", action="store_true", help="remove autostart and installed script")
    ap.add_argument("--last", type=int, metavar="N", help="print the last N characters")
    ap.add_argument("--blocks", type=int, metavar="N", help="print the last N typing blocks")
    ap.add_argument("--grep", metavar="PATTERN", help="search the buffer (regex, case-insensitive)")
    ap.add_argument("--copy", action="store_true", help="send output to the clipboard")
    ap.add_argument("--status", action="store_true", help="show buffer usage")
    ap.add_argument("--clear", action="store_true", help="delete all buffered text")
    ap.add_argument("--config", action="store_true", help="print config file path")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()

    CFG, cfg_path = load_config()
    base().mkdir(parents=True, exist_ok=True)

    if a.install:
        return install()
    if a.uninstall:
        return uninstall()
    if a.gui:
        return gui()
    if a.config:
        print(cfg_path)
        return 0
    if a.status:
        p = store()
        size = p.stat().st_size if p.exists() else 0
        print(f"{p}  {size}/{CFG['max_bytes']} bytes  (max_age_hours={CFG['max_age_hours']})")
        pid = running_pid()
        print(f"recorder: running (pid {pid})" if pid else "recorder: NOT running")
        return 0
    if a.clear:
        store().unlink(missing_ok=True)
        print("cleared")
        return 0
    if a.grep:
        text = read_all()
        rx = re.compile(a.grep, re.I)
        hits = 0
        for m in rx.finditer(text):
            if hits >= 20:
                print("...more matches truncated")
                break
            print(f"--- @{m.start()} ---")
            print(text[max(0, m.start() - 150):m.end() + 150])
            print()
            hits += 1
        if not hits:
            print("no matches")
        return 0
    if a.blocks:
        picked = split_blocks(read_all())[-a.blocks:]
        if not picked:
            print("nothing buffered")
            return 0
        text = "\n\n".join(f"[{s or 'start'}]\n{b.strip(chr(10))}" for s, b in picked)
        sys.stdout.write(text + "\n")
        if a.copy:
            print("[copied]" if copy_clip(text) else "[no clipboard tool]", file=sys.stderr)
        return 0
    if a.last:
        text = read_all()[-a.last:]
        sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))
        if a.copy:
            print("[copied]" if copy_clip(text) else "[no clipboard tool]", file=sys.stderr)
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main())