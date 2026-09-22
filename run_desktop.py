#!/usr/bin/env python3
"""Cattle & Small Stock System — desktop launcher.

Runs the app on this computer and shows it in a real window. Nothing leaves
the machine: the server listens on 127.0.0.1, which is this computer and
nothing else, and the herd databases sit in your own user folder.

    python run_desktop.py              start it
    python run_desktop.py --install    install what it needs, then start
    python run_desktop.py --browser    use the default browser instead
    python run_desktop.py --port 8600  listen on a particular port
    python run_desktop.py --data DIR   keep the databases in DIR
    python run_desktop.py --key KEY    unlock this computer without anyone
                                       typing the key at the screen

Built as an .exe this same file is the entry point, and the Streamlit server
runs as a second copy of it — see SERVER_FLAG below.

Close the window to stop it.
"""
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP_TITLE = "Cattle & Small Stock System"
APP_NAME = "cattlemanagementapp.py"
FROZEN = getattr(sys, "frozen", False)


def _console_speaks_utf8():
    """Make sure printing cannot be what kills the launcher.

    Windows hands Python whatever code page the machine is set to, and the
    older ones cannot represent a dash or an accent: printing one raises
    UnicodeEncodeError and the launcher dies on a message rather than on a
    fault. Asking for UTF-8, and for unrepresentable characters to be
    replaced rather than raise, removes that whole class of failure. Both
    streams are None in the built windowed .exe, which is why every line is
    guarded.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass                      # older Python, or not a text stream


_console_speaks_utf8()

# When the .exe re-launches itself to be the server, this is how the second
# copy knows not to open a second window and re-launch again.
SERVER_FLAG = "CATTLE_SERVER_CHILD"

STARTUP_TIMEOUT = 120          # seconds to wait for the first page


# ── where things are ──────────────────────────────────────────────────
def resources_dir():
    """The folder holding the app file — beside the .exe when frozen,
    beside this script when run from source."""
    if FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


HERE = resources_dir()
APP_FILE = HERE / APP_NAME
REQUIREMENTS = HERE / "requirements.txt"


def say(message):
    print(f"  {message}", flush=True)


def fail(message):
    """Say so where the user will actually see it — a window when there is no
    console to print to, which is the case for the built .exe."""
    print(message, file=sys.stderr, flush=True)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, str(message), APP_TITLE, 0x10)      # MB_ICONERROR
        except Exception:
            pass
    raise SystemExit(1)


# ── the server half ───────────────────────────────────────────────────
def run_server():
    """Hand this process over to Streamlit. Reached only in the second copy,
    the one started with SERVER_FLAG set."""
    port = os.environ.get("CATTLE_PORT", "8501")
    sys.argv = [
        "streamlit", "run", str(APP_FILE),
        "--server.port", port,
        "--server.address", "127.0.0.1",        # this machine only
        "--server.headless", "true",            # we open the window ourselves
        "--server.fileWatcherType", "none",     # nothing here is being edited
        "--server.enableCORS", "false",
        # The window is not a browser with cookies of its own, so the XSRF
        # token round-trip has nothing to ride on and breaks uploads.
        "--server.enableXsrfProtection", "false",
        "--server.maxUploadSize", "50",
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
    ]
    from streamlit.web import cli as streamlit_cli
    sys.exit(streamlit_cli.main())


# ── helpers ───────────────────────────────────────────────────────────
def free_port(preferred):
    """The first port that is genuinely free, starting at the preferred one."""
    for candidate in range(preferred, preferred + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    fail(f"No free port between {preferred} and {preferred + 39}.")


def wait_until_up(port, timeout=STARTUP_TIMEOUT, child=None):
    """Block until the server answers, or give up. True if it came up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if child is not None and child.poll() is not None:
            return False                        # it died on the way up
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.4)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def missing_packages():
    """Which of the app's imports are not installed. Never anything when
    frozen — the .exe carries them."""
    if FROZEN:
        return []
    needed = [("streamlit", "streamlit"), ("pandas", "pandas"),
              ("altair", "altair"), ("reportlab", "reportlab"),
              ("PIL", "pillow"), ("openpyxl", "openpyxl")]
    absent = []
    for module, package in needed:
        try:
            __import__(module)
        except ImportError:
            absent.append(package)
    return absent


def install_requirements():
    say("Installing what the app needs — this runs once and may take a minute.")
    command = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)] \
        if REQUIREMENTS.is_file() else \
        [sys.executable, "-m", "pip", "install",
         "streamlit>=1.31", "pandas>=1.5", "altair>=5.0",
         "reportlab>=3.6", "pillow>=9.0", "openpyxl>=3.1.2"]
    return subprocess.call(command) == 0


def self_check(report_path=None):
    """Import everything the app imports, and compile the app itself.

    The built .exe has no console, so a missing library shows up as a dialog
    box on the user's machine rather than a failed build. This runs the same
    imports at build time and reports with an exit code, which the Windows
    workflow checks before it will package anything.
    """
    problems = []
    lines = [f"Python {sys.version.split()[0]}",
             f"frozen: {FROZEN}", f"resources: {HERE}", ""]

    modules = ["numpy", "pandas", "altair", "streamlit", "streamlit.web.cli",
               "reportlab", "reportlab.pdfgen.canvas", "PIL", "PIL.Image",
               "openpyxl", "sqlite3", "zoneinfo"]
    for name in modules:
        try:
            module = __import__(name, fromlist=["__version__"])
            version = getattr(module, "__version__", "")
            lines.append(f"  ok   {name} {version}".rstrip())
        except Exception as exc:
            problems.append(f"{name}: {exc.__class__.__name__}: {exc}")
            lines.append(f"  FAIL {name} — {exc}")

    # numpy importing is not the same as numpy working: its C extensions are
    # what go missing, and they only complain when something uses them.
    try:
        import numpy
        assert numpy.arange(6).reshape(2, 3).sum() == 15
        lines.append("  ok   numpy arithmetic")
    except Exception as exc:
        problems.append(f"numpy arithmetic: {exc}")
        lines.append(f"  FAIL numpy arithmetic — {exc}")

    try:
        import io
        import pandas
        frame = pandas.DataFrame({"tag": ["T-001"], "weight": [412.5]})
        assert frame["weight"].mean() == 412.5
        lines.append("  ok   pandas frame")
    except Exception as exc:
        problems.append(f"pandas frame: {exc}")
        lines.append(f"  FAIL pandas frame — {exc}")

    try:
        import io
        from reportlab.pdfgen import canvas
        buffer = io.BytesIO()
        page = canvas.Canvas(buffer)
        page.drawString(40, 40, "ok")
        page.save()
        assert buffer.getvalue()[:5] == b"%PDF-"
        lines.append("  ok   reportlab writes a PDF")
    except Exception as exc:
        problems.append(f"reportlab: {exc}")
        lines.append(f"  FAIL reportlab — {exc}")

    try:
        if APP_FILE.is_file():
            compile(APP_FILE.read_text(encoding="utf-8"), str(APP_FILE), "exec")
            lines.append(f"  ok   {APP_NAME} compiles")
        else:
            problems.append(f"{APP_NAME} is not in the bundle")
            lines.append(f"  FAIL {APP_NAME} is missing")
    except Exception as exc:
        problems.append(f"{APP_NAME}: {exc}")
        lines.append(f"  FAIL {APP_NAME} — {exc}")

    lines.append("")
    lines.append("SELF-CHECK PASSED" if not problems else
                 "SELF-CHECK FAILED:\n  " + "\n  ".join(problems))
    report = "\n".join(lines)

    print(report, flush=True)
    if report_path:
        try:
            Path(report_path).write_text(report, encoding="utf-8")
        except Exception:
            pass
    return 0 if not problems else 1


def data_dir_default():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "HDM Cattle Management"


# ── the window ────────────────────────────────────────────────────────
def open_webview(url):
    """A real application window, drawn by the Edge WebView2 runtime that
    ships with Windows 10 and 11. Returns a callable that blocks until the
    user closes it, or None if pywebview is not here."""
    try:
        import webview
    except ImportError:
        return None

    # WebView2 keeps a profile folder; without somewhere writable to put it,
    # it refuses to start in an installed-to-Program-Files build.
    profile = data_dir_default() / "webview"
    profile.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("WEBVIEW2_USER_DATA_FOLDER", str(profile))

    # Every Download button in the app writes to disk itself in desktop mode,
    # but allow the window's own downloads too so nothing is swallowed.
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass

    try:
        webview.create_window(APP_TITLE, url, width=1380, height=900,
                              min_size=(900, 640), confirm_close=False)
    except Exception as exc:
        say(f"The app window could not be created ({exc}); using the browser.")
        return None

    def start():
        try:
            webview.start(private_mode=False, debug=False)
        except TypeError:
            webview.start()

    return start


def open_app_window_via_edge(url):
    """No pywebview: a Chromium browser in app mode still gives a plain
    window with no address bar, which is most of the point."""
    if os.name != "nt":
        return False
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for browser in candidates:
        if os.path.isfile(browser):
            try:
                subprocess.Popen([browser, f"--app={url}",
                                  "--window-size=1380,900"])
                return True
            except Exception:
                continue
    return False


def open_window(url, prefer_browser):
    """Returns a callable that waits for the window to close, or None when
    there is nothing to wait on."""
    if not prefer_browser:
        start = open_webview(url)
        if start is not None:
            say("Opening the app window.")
            return start
        if open_app_window_via_edge(url):
            say("Opening the app window.")
            return None
    say("Opening the app in your browser.")
    webbrowser.open(url)
    return None


# ── main ──────────────────────────────────────────────────────────────
def parse_args(argv):
    import argparse
    parser = argparse.ArgumentParser(
        prog="run_desktop", description=APP_TITLE + " — desktop launcher")
    parser.add_argument("--install", action="store_true",
                        help="install the packages the app needs, then start")
    parser.add_argument("--browser", action="store_true",
                        help="use the default browser instead of an app window")
    parser.add_argument("--port", type=int, default=8501,
                        help="port to listen on (default 8501)")
    parser.add_argument("--data", metavar="DIR",
                        help="folder to keep the herd databases in")
    parser.add_argument("--key", metavar="KEY",
                        help="access key, for unlocking this computer without "
                             "anyone typing it in")
    parser.add_argument("--selfcheck", action="store_true",
                        help="check that everything the app needs is here, "
                             "report, and exit — used by the Windows build")
    parser.add_argument("--report", metavar="FILE",
                        help="with --selfcheck, also write the report to FILE")
    return parser.parse_args(argv)


def main():
    # Second copy of ourselves: be the server and nothing else.
    if os.environ.get(SERVER_FLAG) == "1":
        run_server()
        return

    args = parse_args(sys.argv[1:])

    print(f"\n{APP_TITLE}\n" + "=" * len(APP_TITLE))

    if args.selfcheck:
        raise SystemExit(self_check(args.report))

    if not APP_FILE.is_file():
        fail(f"Cannot find {APP_NAME} — it must sit beside this launcher.")
    if sys.version_info < (3, 9):
        fail("This needs Python 3.9 or newer; you have "
             f"{sys.version.split()[0]}.")

    absent = missing_packages()
    if absent and (args.install or not sys.stdin or not sys.stdin.isatty()):
        if not install_requirements():
            fail("The install did not finish. Run it by hand:\n"
                 f"  {sys.executable} -m pip install -r requirements.txt")
        absent = missing_packages()
    if absent:
        fail("These packages are missing: " + ", ".join(absent) + "\n"
             "Install them with:\n"
             f"  {sys.executable} -m pip install -r requirements.txt\n"
             "or start the app with:  python run_desktop.py --install")

    # Run from the app folder so .streamlit/config.toml is picked up.
    os.chdir(str(HERE))

    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}"

    environment = dict(os.environ)
    environment[SERVER_FLAG] = "1"
    environment["CATTLE_PORT"] = str(port)
    # Downloads are written straight to disk rather than handed to a browser.
    environment["CATTLE_DESKTOP"] = "1"
    environment.setdefault("PYTHONIOENCODING", "utf-8")

    if args.data:
        data_dir = Path(args.data).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        environment["CATTLE_DB_PATH"] = str(data_dir / "cattle.db")
        environment["GOAT_DB_PATH"] = str(data_dir / "goats.db")
        environment["SHEEP_DB_PATH"] = str(data_dir / "sheep.db")
        environment["PIG_DB_PATH"] = str(data_dir / "pigs.db")
        say(f"Herd databases: {data_dir}")

    # Unlocking without anyone at the keyboard: hand the key to the app,
    # which checks it and records the unlock the same way the screen does.
    if args.key:
        environment["CATTLE_ACCESS_KEY"] = args.key
        say("Access key supplied — this computer will unlock without asking.")

    # Frozen: re-launch the .exe, which lands in run_server() above. From
    # source: the same file, run by the same interpreter.
    command = [sys.executable] if FROZEN else [sys.executable, __file__]

    say(f"Starting on {url}")
    child = subprocess.Popen(
        command, env=environment, cwd=str(HERE),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt" and FROZEN else 0)
    try:
        if not wait_until_up(port, child=child):
            child.terminate()
            fail("The app did not start. Run it directly to see why:\n"
                 f"  {sys.executable} -m streamlit run {APP_NAME}")

        wait_for_close = open_window(url, args.browser)
        print()
        say("The app is running. Close the window to stop it.")
        if wait_for_close is not None:
            wait_for_close()                    # returns when the window closes
        else:
            child.wait()
    except KeyboardInterrupt:
        print()
        say("Stopping.")
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        say("Stopped.")


if __name__ == "__main__":
    main()
