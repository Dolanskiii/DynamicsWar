"""Starts DynamicsWar: local server plus an app window.

No extra packages: the window is Microsoft Edge (or Chrome) in app mode, which gives a
plain window without tabs or address bar. Without such a browser the default browser opens.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__, config, demo, server

PORT = 48620            # fixed, so a second start finds the running app instead of starting another
REPLACE_WAIT = 10       # seconds a start waits for a running instance of another version to end
SESSION_RE = re.compile(rb'<meta name="session" content="([^"]+)">')
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))       # never through a proxy
IDLE_LIMIT = 180        # seconds without any sign of the window before the server stops
BYE_GRACE = 6           # a closing window says goodbye; a reload reports back within this time
EXIT_GRACE = 4


def find_browser():
    """Path of a Chromium-based browser that supports --app, or None."""
    names = []
    if sys.platform == "win32":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for root in filter(None, roots):
            names += [Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                      Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"]
    elif sys.platform == "darwin":
        names += [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                  Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    for path in names:
        if Path(path).is_file():
            return str(path)
    for exe in ("msedge", "microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def running_instance(port):
    """(address, session) of a DynamicsWar already listening on the port, or None."""
    url = f"http://127.0.0.1:{port}/"
    try:
        with LOCAL.open(url, timeout=2) as resp:
            page = resp.read(4000)
    except OSError:
        return None
    if b"<title>DynamicsWar</title>" not in page:
        return None
    found = SESSION_RE.search(page)
    return url, found.group(1).decode("ascii", "replace") if found else ""


def ask_instance(url, path, session, post=False):
    """Answer of a running instance as (status, data); (None, None) when it does not answer."""
    req = urllib.request.Request(url.rstrip("/") + path, data=b"{}" if post else None,
                                 headers={"X-Session": session, "Content-Type": "application/json"})
    try:
        with LOCAL.open(req, timeout=3) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return None, None


def instance_version(url, session):
    """Version of the running instance, or None when it cannot be asked."""
    status, data = ask_instance(url, "/api/status", session)
    return data.get("version") if status == 200 and isinstance(data, dict) else None


def replace_instance(url, session, port, wait=None):
    """Asks a running instance to end and waits until it has. True when the port is free.

    Versions up to 0.7.0 do not know the request. They end by themselves a few seconds after
    their window said goodbye - so that is sent instead, and it works when no window is open."""
    status, _ = ask_instance(url, "/api/quit", session, post=True)
    if status != 200:
        ask_instance(url, "/api/bye", session, post=True)
    end = time.monotonic() + (REPLACE_WAIT if wait is None else wait)
    while time.monotonic() < end:
        time.sleep(0.3)
        if not running_instance(port):
            return True
    return False


def open_window(url, profile="window-profile"):
    """Opens the interface. Returns the browser process if we own one, else None."""
    exe = find_browser()
    if exe:
        profile = config.config_dir() / profile             # own profile: own window, own process
        profile.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}",
                                     "--start-maximized", "--window-size=1560,980", "--no-first-run",
                                     "--no-default-browser-check"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            maximise_window(proc)
            return proc
        except OSError:
            pass
    webbrowser.open(url)
    return None


def maximise_window(proc, seconds=15.0):
    """Windows: Edge reopens an app window in the size it had last and ignores --start-maximized
    for it. So the window is maximised through the Windows API as soon as it exists. Found by
    the process that owns it or, when Edge hands the start over to a running process, by its
    title. Runs in the background; a failure only means the window keeps its size."""
    if sys.platform != "win32":
        return

    def work():
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            each_window = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [each_window, wintypes.LPARAM]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsZoomed.argtypes = [wintypes.HWND]
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

            def find():
                hits = []

                def each(hwnd, _):
                    if user32.IsWindowVisible(hwnd):
                        name = ctypes.create_unicode_buffer(64)
                        user32.GetClassNameW(hwnd, name, 64)
                        title = ctypes.create_unicode_buffer(256)
                        user32.GetWindowTextW(hwnd, title, 256)
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if name.value == "Chrome_WidgetWin_1" and title.value and (
                                pid.value == proc.pid or title.value == "DynamicsWar"):
                            hits.append(hwnd)
                    return True

                user32.EnumWindows(each_window(each), 0)
                return hits

            end = time.monotonic() + seconds
            while time.monotonic() < end:
                hits = find()
                if hits:
                    for hwnd in hits:
                        if not user32.IsZoomed(hwnd):
                            user32.ShowWindow(hwnd, 3)          # SW_MAXIMIZE
                    return
                time.sleep(0.25)
        except Exception:
            pass

    threading.Thread(target=work, name="dynamicswar-maximise", daemon=True).start()


def serve_on(state, port, patient=False, tries=20):
    """Starts the server on the port. patient: the port was just given up by another instance and
    the system may need a moment to release it."""
    for attempt in range(tries if patient else 1):
        try:
            return server.serve(state, port)
        except OSError:
            if attempt + 1 >= (tries if patient else 1):
                raise
            time.sleep(0.25)


def wait_until_closed(state, proc, started):
    """Blocks while the window is in use."""
    last_tick = time.monotonic()
    while True:
        time.sleep(1)
        if state.quit:
            return
        now = time.monotonic()
        if now - last_tick > 20:                # computer was asleep: start counting afresh
            state.last_seen = now
        last_tick = now
        idle = now - state.last_seen
        if state.bye_at and now - state.bye_at > BYE_GRACE:
            return
        if proc and proc.poll() is not None:
            if now - started < 5:               # handed over to a running browser: not our window
                proc = None
            elif idle > EXIT_GRACE:
                return
        if idle > IDLE_LIMIT:
            return


def log_error(text):
    try:
        path = config.config_dir() / "dynamicswar.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{text}\n")
        return path
    except OSError:
        return None


def alert(text):
    """A start-up problem must be visible even without a console (pythonw)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "DynamicsWar", 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(prog="dynamicswar",
                                description="Finds the best-sounding edition of an album on Qobuz or Tidal.")
    p.add_argument("--demo", action="store_true", help="run with the built-in demo catalogue (no account)")
    p.add_argument("--service", choices=("qobuz", "tidal"), default=None,
                   help="streaming service for this start (default: the one from the settings, else Qobuz)")
    p.add_argument("--port", type=int, default=None,
                   help=f"port of the local server (default {PORT}; 0 = any free port)")
    p.add_argument("--no-window", action="store_true", help="only print the address, do not open a window")
    p.add_argument("--version", action="version", version=f"DynamicsWar {__version__}")
    args = p.parse_args(argv)
    # a start with a named service gets a port of its own, so it can run next to the normal one
    own = PORT + 1 if args.service == "tidal" else PORT
    profile = "window-profile-tidal" if args.service == "tidal" else "window-profile"
    port = args.port if args.port is not None else (0 if args.demo else own)
    took_over = False
    if port and not args.demo:
        existing = running_instance(port)
        if existing:
            url, session = existing
            version = instance_version(url, session)
            if version in (None, __version__):  # already running: just show its window again
                if args.no_window:
                    print(url, flush=True)
                else:
                    open_window(url, profile)
                return 0
            # another version is still running (the app was updated while it was open): it gives
            # way to this start, otherwise the old version would silently stay in use
            if not replace_instance(url, session, port):
                alert(f"DynamicsWar {version} is still open.\n"
                      f"Close its window and start again to use version {__version__}.")
                return 1
            took_over = True
    try:
        if args.demo:
            state = server.State(cfg={}, save=lambda cfg: None, fetch=demo.DemoQobuz(), demo=True)
        else:
            state = server.State(report_dir=Path(__file__).resolve().parent.parent / "debug",
                                 service=args.service)
        try:
            httpd, url = serve_on(state, port, patient=took_over)
        except OSError:
            if args.port is not None:
                raise
            httpd, url = server.serve(state, 0)     # port taken by something else
    except Exception:
        path = log_error(traceback.format_exc())
        alert("DynamicsWar could not start." + (f"\nDetails: {path}" if path else ""))
        return 1
    started = time.monotonic()
    if args.no_window:
        print(url, flush=True)
        proc = None
    else:
        proc = open_window(url, profile)
    try:
        if args.no_window:
            while not state.quit:
                time.sleep(1)
        else:
            wait_until_closed(state, proc, started)
    except KeyboardInterrupt:
        pass
    finally:
        state.closing = True            # work in the background ends at its next step
        httpd.shutdown()
        httpd.server_close()            # frees the port at once for a start that takes over
    return 0
