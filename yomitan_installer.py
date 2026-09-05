"""
yomitan_installer.py - One-click Yomitan bridge installer for CompreDef.

Bundles the yomitan-api native host (yomitan_api.py) and installs the
required NativeMessagingHosts manifests for Chrome/Firefox/Brave/Edge
so that Yomitan's "Enable Yomitan API" toggle actually exposes
http://127.0.0.1:19633 without the user ever opening a terminal.

This replicates the logic of `install_yomitan_api.py` from
https://github.com/yomidevs/yomitan-api but runs automatically from
Anki's Python when the user clicks "Install / Repair Bridge" in the
CompreDef config dialog.

No external dependencies, no git clone, no terminal.
"""

import copy
import json
import os
import shutil
import sys
import re

# ---------------------------------------------------------------------------
# Bridge script content (yomitan_api.py, based on yomidevs/yomitan-api with
# CompreDef's MV3 service-worker fixes — see KEEPALIVE notes below).
# This is written to user_files/yomitan_bridge/yomitan_api.py on install.
#
# WHY THIS IS NOT A VANILLA UPSTREAM COPY ANYMORE:
# Upstream's host blocks reading stdin and never keeps Yomitan's MV3 service
# worker (SW) alive. On Chrome 151 we verified live that the SW suspends
# ~30s after connectNative, Chrome closes both pipes, and the host process
# turns into a ZOMBIE holding port 19633 while every /ankiFields returns 502
# ("Yomitan completely dead" incident, 2026-09-04). Our additions:
#   1. Keepalive thread — pings the native port every 20s. Per Chrome docs
#      (Chrome 105/110/114 rules), receiving a message on the port resets
#      the SW's 30-second idle timer, so the SW (and the port) stay alive
#      for as long as Chrome runs. Yomitan's SW just posts the ping back.
#   2. Reader thread owns stdin — a background thread blocks on stdin. On
#      EOF (SW suspended / Chrome quit) it fails all pending requests and
#      cleanly shuts the HTTP server down, so the host EXITS and frees port
#      19633 for the next SW cold start (which re-launches the host). No
#      more zombie port-holder.
#   3. Nonce pairing — every request carries a unique "_cd" nonce in
#      params; Yomitan echoes params back in its reply. Pending requests
#      match replies by nonce, so keepalive echoes and stale replies can
#      never be mis-paired with an HTTP request's response.
#   4. Timeout bump — YOMITAN_RESPONSE_TIMEOUT 2.0 -> 8.0s: a cold first
#      query after browser start legitimately takes seconds (SW boot +
#      dictionary DB open). Anki-side fetch timeout must exceed this
#      (yomitan.py uses 10s).
# ---------------------------------------------------------------------------
BRIDGE_SCRIPT = r'''#!/usr/bin/env -S python3 -u

import datetime
import http.server
import json
import os
import signal
import struct
import sys
import threading
import time
import traceback
import urllib

ADDR = "127.0.0.1"
PORT = 19633
PROCESS_STARTUP_WAIT = 5
YOMITAN_RESPONSE_TIMEOUT = 2.0

# SW idle timer is 30s (Chrome docs). Ping well under it, with margin for
# scheduling jitter. Each ping fires port.onMessage in Yomitan's service
# worker, which resets the idle timer (Chrome 105/110/114 rules).
KEEPALIVE_INTERVAL = 20.0

YOMITAN_API_NATIVE_MESSAGING_VERSION = 1
BLACKLISTED_PATHS = ["favicon.ico"]

script_path = os.path.realpath(os.path.dirname(__file__))
crowbarfile_path = script_path + "/.crowbar"

def error_log(message: str, error: str = "") -> None:
    try:
        utc_time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        with open(script_path + "/error.log", "a", encoding = "utf8") as log_file:
            log_file.write(utc_time + ", " + str(message).replace("\r", r"\r").replace("\n", r"\n") + ", " + str(error).replace("\r", r"\r").replace("\n", r"\n").replace("\n", r"\n") + "\n")
    except Exception:
        pass

def ensure_single_instance() -> None:
    wait_time = 0
    try:
        with open(crowbarfile_path, "r") as crowbarfile:
            os.kill(int(crowbarfile.read()), signal.SIGTERM)
            wait_time = PROCESS_STARTUP_WAIT
    except Exception:
        error_log(traceback.format_exc())

    with open(crowbarfile_path, "w") as crowbarfile:
        crowbarfile.write(str(os.getpid()))

    time.sleep(wait_time)

def delete_crowbarfile() -> None:
    try:
        os.remove(crowbarfile_path)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Yomitan connection state, shared between the reader/keepalive threads and
# the HTTP request threads.
# ---------------------------------------------------------------------------
_stdin_lock = threading.Lock()      # serializes ALL stdin reads
_stdout_lock = threading.Lock()     # serializes ALL stdout writes
_yomitan_connected = threading.Event()  # set while stdin is alive (port open)

_pending_lock = threading.Lock()
_pending: dict = {}                 # nonce -> threading.Event
_responses: dict = {}               # nonce -> reply dict or None (timeout)

def _raw_send(message_content: dict) -> bool:
    """Writes one native-messaging frame to stdout. Returns False if the
    pipe is broken (SW suspended / Chrome quit) — callers must then treat
    Yomitan as disconnected."""
    try:
        encoded_content = json.dumps(message_content).encode("utf-8")
        encoded_length = struct.pack("@I", len(encoded_content))
        with _stdout_lock:
            sys.stdout.buffer.write(encoded_length)
            sys.stdout.buffer.write(encoded_content)
            sys.stdout.buffer.flush()
        return True
    except Exception:
        error_log(traceback.format_exc())
        return False

def _stdin_reader() -> None:
    """Thread: the ONLY consumer of stdin. Reads frames until EOF.

    EOF means the SW suspended and closed the port (or Chrome quit) —
    once that happens no reply can ever arrive, so we fail every pending
    request and shut the whole host down. This is what prevents the
    zombie-host-holding-port-19633 failure mode.
    """
    while True:
        try:
            raw_length = sys.stdin.buffer.read(4)
        except Exception:
            break
        if not raw_length or len(raw_length) < 4:
            break  # EOF: port closed
        try:
            message_length = struct.unpack("@I", raw_length)[0]
            if message_length > 10 * 1024 * 1024:
                continue  # refuse oversized frames, keep reading
            message = sys.stdin.buffer.read(message_length).decode("utf-8")
            reply = json.loads(message)
        except Exception:
            error_log(traceback.format_exc())
            continue
        if not isinstance(reply, dict):
            continue
        # Route by echoed nonce; unknown nonce replies (e.g. keepalive
        # echoes) are dropped instead of mis-paired.
        params = reply.get("params")
        nonce = None
        if isinstance(params, dict):
            nonce = params.get("_cd")
        if nonce is not None:
            with _pending_lock:
                ev = _pending.get(nonce)
                if ev is not None:
                    _responses[nonce] = reply
                    ev.set()

    # stdin EOF — Yomitan is gone. Fail everything pending, mark the host
    # dead so HTTP callers return 502 instead of hanging, and shut down the
    # HTTP server so this process EXITS and frees port 19633 for the next
    # browser launch (this is the anti-zombie guarantee).
    _yomitan_connected.clear()
    with _pending_lock:
        for ev in _pending.values():
            ev.set()
    _shutdown_host()

def _keepalive_loop() -> None:
    """Thread: pings Yomitan's service worker every KEEPALIVE_INTERVAL.

    Receiving a message on the native port resets the SW's 30s idle timer
    (Chrome 105/110/114 lifecycle rules), preventing the suspend that
    otherwise closes our pipes ~30s after connectNative. Yomitan's SW
    replies with action "keepalive" + 400 (unknown action), which the
    reader drops as an unknown nonce. If the pipe is broken we stop —
    the reader thread will have already begun host shutdown.
    """
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        if not _yomitan_connected.is_set():
            return
        if not _raw_send({"action": "keepalive", "params": {"_cd": "keepalive"}, "body": ""}):
            return

def send_message(message_content: dict) -> bool:
    """Public send kept for source compatibility with upstream layout;
    reports pipe health."""
    return _raw_send(message_content)

def request_yomitan(action: str, params: dict, body: str, timeout: float) -> dict:
    """Sends one request to Yomitan and waits for its nonce-matched reply.

    Returns the reply dict, or None on timeout / disconnection.
    """
    nonce = f"{time.time_ns()}-{threading.get_ident()}"
    frame = {"action": action, "params": {**params, "_cd": nonce}, "body": body}
    ev = threading.Event()
    with _pending_lock:
        _pending[nonce] = ev

    if not _raw_send(frame):
        with _pending_lock:
            _pending.pop(nonce, None)
        return None

    ev.wait(timeout + 0.5)
    with _pending_lock:
        _pending.pop(nonce, None)
        reply = _responses.pop(nonce, None)
    return reply

def send_response(request_handler, status_code: int, content_type: str, data: str) -> None:
    request_handler.send_response(status_code)
    request_handler.send_header("Content-type", content_type)
    request_handler.send_header("Access-Control-Allow-Origin", "*")
    request_handler.send_header("Access-Control-Allow-Methods", "*")
    request_handler.send_header("Access-Control-Allow-Headers", "*")
    request_handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    request_handler.end_headers()
    try:
        request_handler.wfile.write(bytes(data, "utf-8"))
    except Exception:
        pass

def handle_invalid_method(request_handler) -> None:
    request_handler.send_error(405, str(request_handler.command) + " method not allowed, only POST is accepted")
    request_handler.send_header("Allow", "POST")
    request_handler.end_headers()

httpd = None

class RequestHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path[1:]
        params = urllib.parse.parse_qs(parsed_url.query)
        content_length = int(self.headers["Content-Length"] or 0)
        try:
            body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        except Exception:
            body = ""

        if path in BLACKLISTED_PATHS:
            send_response(self, 400, "", "")
            return

        if path in ["serverVersion", ""]:
            send_response(self, 200, "application/json", json.dumps({"version": YOMITAN_API_NATIVE_MESSAGING_VERSION}))
            return

        # Full-chain endpoints (yomitanVersion/termEntries/ankiFields/...)
        # require the browser half: SW alive + Yomitan answering via stdin.
        try:
            if not _yomitan_connected.is_set():
                send_response(self, 502, "application/json", json.dumps({"error": "Yomitan not connected (browser closed or API disabled). Ensure browser is open, Yomitan API enabled, and bridge installed."}))
                return
            yomitan_response = request_yomitan(path, params, body, YOMITAN_RESPONSE_TIMEOUT)
            if yomitan_response is None:
                send_response(self, 502, "application/json", json.dumps({"error": "Yomitan not connected (browser closed or API disabled). Ensure browser is open, Yomitan API enabled, and bridge installed."}))
                return
            send_response(self, yomitan_response.get("responseStatusCode", 200), "application/json", json.dumps(yomitan_response.get("data"), ensure_ascii = False))
        except Exception:
            error_log(traceback.format_exc())
            try:
                send_response(self, 500, "application/json", json.dumps({"error": "bridge error"}))
            except Exception:
                pass

    do_GET = handle_invalid_method
    do_HEAD = handle_invalid_method
    do_PUT = handle_invalid_method
    do_DELETE = handle_invalid_method
    do_CONNECT = handle_invalid_method
    do_OPTIONS = handle_invalid_method
    do_TRACE = handle_invalid_method
    do_PATCH = handle_invalid_method

def _shutdown_host() -> None:
    """Stops the HTTP server so serve_forever() returns and the process
    exits, releasing port 19633 for the next launch."""
    global httpd
    try:
        if httpd is not None:
            httpd.shutdown()
    except Exception:
        error_log(traceback.format_exc())

try:
    ensure_single_instance()
    httpd = http.server.ThreadingHTTPServer((ADDR, PORT), RequestHandler)
    _yomitan_connected.set()
    threading.Thread(target=_stdin_reader, daemon=True).start()
    threading.Thread(target=_keepalive_loop, daemon=True).start()
    httpd.serve_forever()
    delete_crowbarfile()
except Exception:
    error_log(traceback.format_exc())
    delete_crowbarfile()
finally:
    # Belt-and-braces: the process must never linger with a dead port.
    try:
        if httpd is not None:
            httpd.server_close()
    except Exception:
        pass
'''

NAME = "yomitan_api"

MANIFEST_TEMPLATE = {
    "name": "yomitan_api",
    "description": "Yomitan API",
    "type": "stdio",
}

BROWSER_DATA = {
    "firefox": {
        "extension_id_key": "allowed_extensions",
        "extension_ids": ["{6b733b82-9261-47ee-a595-2dda294a4d08}"],
        "extension_id_format": r"{xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx} or testextension@example.com",
        "regex_test": r"(?:\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}|[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+)"
    },
    "chrome": {
        "extension_id_key": "allowed_origins",
        "extension_ids": ["chrome-extension://likgccmbimhjbgkjambclfkhldnlhbnn/"],
        "extension_id_format": "chrome-extension://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/",
        "regex_test": r"chrome-extension:\/\/[a-p]{32}\/"
    },
    "chromium": {
        "extension_id_key": "allowed_origins",
        "extension_ids": ["chrome-extension://likgccmbimhjbgkjambclfkhldnlhbnn/"],
        "extension_id_format": "chrome-extension://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/",
        "regex_test": r"chrome-extension:\/\/[a-p]{32}\/"
    },
    "edge": {
        "extension_id_key": "allowed_origins",
        "extension_ids": ["chrome-extension://likgccmbimhjbgkjambclfkhldnlhbnn/"],
        "extension_id_format": "chrome-extension://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/",
        "regex_test": r"chrome-extension:\/\/[a-p]{32}\/"
    },
    "brave": {
        "extension_id_key": "allowed_origins",
        "extension_ids": ["chrome-extension://likgccmbimhjbgkjambclfkhldnlhbnn/"],
        "extension_id_format": "chrome-extension://xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/",
        "regex_test": r"chrome-extension:\/\/[a-p]{32}\/"
    },
}

PLATFORM_DATA = {
    "linux": {
        "platform_aliases": ["linux", "linux2", "riscos", "freebsd7", "freebsd8", "freebsdN", "openbsd6"],
        "manifest_install_data": {
            "firefox": {
                "methods": ["file"],
                "path": os.path.expanduser("~/.mozilla/native-messaging-hosts/"),
            },
            "chrome": {
                "methods": ["file"],
                "path": os.path.expanduser("~/.config/google-chrome/NativeMessagingHosts/"),
            },
            "chromium": {
                "methods": ["file"],
                "path": os.path.expanduser("~/.config/chromium/NativeMessagingHosts/"),
            },
            "brave": {
                "methods": ["file"],
                "path": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts/"),
            },
        },
    },
    "windows": {
        "platform_aliases": ["win32", "cygwin"],
        "manifest_install_data": {
            "firefox": {
                "methods": ["file", "registry"],
                "path": None,  # filled dynamically
                "registry_path": f"SOFTWARE\\Mozilla\\NativeMessagingHosts\\{NAME}",
            },
            "chrome": {
                "methods": ["file", "registry"],
                "path": None,
                "registry_path": f"SOFTWARE\\Google\\Chrome\\NativeMessagingHosts\\{NAME}",
            },
            "chromium": {
                "methods": ["file", "registry"],
                "path": None,
            },
            "edge": {
                "methods": ["file", "registry"],
                "path": None,
                "registry_path": f"SOFTWARE\\Microsoft\\Edge\\NativeMessagingHosts\\{NAME}",
            },
            "brave": {
                "methods": ["file", "registry"],
                "path": None,
                "registry_path": f"SOFTWARE\\BraveSoftware\\Brave-Browser\\NativeMessagingHosts\\{NAME}",
            },
        },
    },
    "mac": {
        "platform_aliases": ["darwin"],
        "manifest_install_data": {
            "firefox": {
                "methods": ["file"],
                "path": os.path.expanduser("~/Library/Application Support/Mozilla/NativeMessagingHosts/"),
            },
            "chrome": {
                "methods": ["file"],
                "path": os.path.expanduser("~/Library/Application Support/Google/Chrome/NativeMessagingHosts/"),
            },
            "chromium": {
                "methods": ["file"],
                "path": os.path.expanduser("~/Library/Application Support/Chromium/NativeMessagingHosts/"),
            },
            "brave": {
                "methods": ["file"],
                "path": os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts/"),
            },
        },
    },
}

def _get_bridge_dir() -> str:
    """Returns the persistent bridge directory inside CompreDef's user_files."""
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    bridge_dir = os.path.join(addon_dir, "user_files", "yomitan_bridge")
    os.makedirs(bridge_dir, exist_ok=True)
    return bridge_dir

def _ensure_bridge_script() -> str:
    """Writes the bundled bridge script to user_files and returns its path."""
    bridge_dir = _get_bridge_dir()
    script_path = os.path.join(bridge_dir, "yomitan_api.py")
    # Always overwrite to ensure we ship the latest version
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(BRIDGE_SCRIPT)
    try:
        os.chmod(script_path, 0o755)
    except Exception:
        pass
    return script_path

def _platform_data_get() -> dict:
    for platform_name in PLATFORM_DATA:
        data = copy.deepcopy(PLATFORM_DATA[platform_name])
        data["platform"] = platform_name
        if sys.platform in data["platform_aliases"]:
            return data
    # Fallback to linux for unknown
    data = copy.deepcopy(PLATFORM_DATA["linux"])
    data["platform"] = "linux"
    return data

def _manifest_get(browser: str, messaging_host_path: str, additional_ids=None) -> str:
    if additional_ids is None:
        additional_ids = []
    manifest = copy.deepcopy(MANIFEST_TEMPLATE)
    data = BROWSER_DATA[browser]
    manifest["path"] = messaging_host_path
    manifest[data["extension_id_key"]] = []
    for extension_id in data["extension_ids"] + additional_ids:
        manifest[data["extension_id_key"]].append(extension_id)
    return json.dumps(manifest, indent=4)

def _manifest_install_file(manifest: str, path: str) -> None:
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, NAME + ".json"), "w", encoding="utf-8") as f:
        f.write(manifest)

def _is_browser_launched(pid: int) -> bool:
    """True if the process was launched by the browser via native messaging.

    The browser appends the caller origin to argv, e.g.
        yomitan_api.py chrome-extension://likgccmbimhjbgkjambclfkhldnlhbnn/
    A standalone bridge has a bare cmdline with no origin argument.
    We must NEVER kill browser-launched instances (v1.0.34 incident:
    a killer matching on yomitan_api.py alone murdered a live connection).
    Zombie browser-launched hosts are reclaimed by the crowbar takeover in
    ensure_single_instance() when the next instance starts — not by us.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().decode(errors="ignore")
    except Exception:
        # Unreadable (already dead, or not Linux) — assume browser-launched
        # so we err on the side of NOT killing.
        return True
    return "chrome-extension://" in cmd or "moz-extension://" in cmd


def _kill_standalone_bridge() -> str:
    """Kills any standalone bridge we previously autostarted.

    A standalone bridge holds port 19633 with stdin=/dev/null, so EVERY
    /ankiFields returns 502 and — worse — the browser-launched bridge
    cannot bind (Address already in use). After removing autostart we must
    clean up orphans from older versions.

    ONLY bare-cmdline (standalone) processes are killed; browser-launched
    ones are left to the crowbar takeover (see _is_browser_launched).
    """
    import subprocess
    killed = []
    try:
        # Find PIDs listening on 19633 via /proc (Linux) or lsof fallback
        if sys.platform != "win32":
            try:
                out = subprocess.run(
                    ["fuser", "19633/tcp"],
                    capture_output=True, text=True, timeout=3,
                )
                # fuser prints PIDs to stderr
                pids = set()
                for chunk in (out.stdout + " " + out.stderr).split():
                    chunk = chunk.strip().strip(",")
                    if chunk.isdigit():
                        pids.add(int(chunk))
                for pid in pids:
                    try:
                        with open(f"/proc/{pid}/cmdline", "rb") as f:
                            cmd = f.read().decode(errors="ignore")
                        if "yomitan_api.py" in cmd and not _is_browser_launched(pid):
                            os.kill(pid, 15)
                            killed.append(pid)
                    except Exception:
                        continue
            except Exception:
                pass
            # Fallback: kill by cmdline scan (standalone only)
            if not killed:
                try:
                    out = subprocess.run(
                        ["pgrep", "-f", "yomitan_api.py"],
                        capture_output=True, text=True, timeout=3,
                    )
                    for line in out.stdout.splitlines():
                        line = line.strip()
                        if line.isdigit():
                            pid = int(line)
                            try:
                                if _is_browser_launched(pid):
                                    continue
                                os.kill(pid, 15)
                                killed.append(pid)
                            except Exception:
                                continue
                except Exception:
                    pass
        # Remove stale crowbar so next browser launch doesn't SIGTERM a dead PID
        # (it handles missing file already, but a stale PID pointing at an
        # unrelated reused PID would be bad — remove it).
        try:
            bridge_dir = _get_bridge_dir()
            crowbar = os.path.join(bridge_dir, ".crowbar")
            if os.path.isfile(crowbar):
                os.remove(crowbar)
        except Exception:
            pass
    except Exception:
        pass
    if killed:
        return f"killed stale standalone bridge PID(s) {killed} holding port 19633"
    return "no stale standalone bridge found"


def install_bridge(additional_extension_ids=None) -> dict:
    """Installs the Yomitan bridge for all detected browsers.

    Returns dict browser->(success bool, message).
    Never raises — Anki must never crash because install failed.
    """
    if additional_extension_ids is None:
        additional_extension_ids = []
    results = {}
    try:
        script_path = _ensure_bridge_script()
    except Exception as e:
        return {"error": (False, f"Failed to write bridge script: {e}")}

    platform_data = _platform_data_get()
    bridge_dir = _get_bridge_dir()

    # For Windows, manifest path is bridge_dir itself
    if platform_data["platform"] == "windows":
        for browser in PLATFORM_DATA["windows"]["manifest_install_data"]:
            PLATFORM_DATA["windows"]["manifest_install_data"][browser]["path"] = bridge_dir

    for browser, install_data in platform_data["manifest_install_data"].items():
        # Try to install even if browser not detected — manifest dir creation is harmless
        # But we can skip if neither chrome nor firefox dir exists? No, create anyway.
        try:
            # Handle mac special case: script is copied into manifest path
            effective_script_path = script_path
            if platform_data["platform"] == "mac":
                effective_script_path = os.path.join(install_data["path"], "yomitan_api.py")
                try:
                    shutil.copy(script_path, effective_script_path)
                except Exception as ce:
                    results[browser] = (False, f"copy failed: {ce}")
                    continue
            elif platform_data["platform"] == "windows":
                bat_path = os.path.join(bridge_dir, "yomitan_api.bat")
                try:
                    with open(bat_path, "w", encoding="utf-8", newline="\n") as f:
                        f.write(f'@echo off\n"{sys.executable}" -u "{script_path}"')
                    effective_script_path = bat_path
                except Exception as be:
                    results[browser] = (False, f"bat write failed: {be}")
                    continue

            manifest = _manifest_get(browser, effective_script_path, additional_extension_ids)
            for method in install_data["methods"]:
                if method == "file":
                    try:
                        _manifest_install_file(manifest, install_data["path"])
                        results[browser] = (True, f"installed to {install_data['path']}")
                    except Exception as fe:
                        results[browser] = (False, f"file manifest failed: {fe}")
                elif method == "registry":
                    try:
                        import winreg
                        winreg.CreateKey(winreg.HKEY_CURRENT_USER, install_data["registry_path"])
                        registry_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, install_data["registry_path"], 0, winreg.KEY_WRITE)
                        winreg.SetValueEx(registry_key, "", 0, winreg.REG_SZ, os.path.join(install_data["path"], NAME + ".json"))
                        winreg.CloseKey(registry_key)
                        # Don't overwrite file result if already success
                        if browser not in results or not results[browser][0]:
                            results[browser] = (True, f"registry {install_data['registry_path']}")
                    except Exception as re:
                        # Registry write may fail without admin on some setups, but file method may have succeeded
                        if browser not in results:
                            results[browser] = (False, f"registry failed: {re}")
        except Exception as e:
            results[browser] = (False, f"unexpected: {e}")

    # DO NOT autostart a standalone bridge here. A standalone instance holds
    # port 19633 with stdin=/dev/null, so /serverVersion looks green while
    # EVERY /ankiFields returns 502 — and the browser-launched bridge can't
    # bind (Address already in use). The browser must own the port.
    # Instead, kill orphans from older CompreDef versions that did autostart.
    try:
        results["_cleanup"] = (True, _kill_standalone_bridge())
    except Exception as e:
        results["_cleanup"] = (False, f"cleanup failed: {e}")

    return results

def get_install_status() -> dict:
    """Checks which browsers have a manifest installed."""
    platform_data = _platform_data_get()
    status = {}
    for browser, install_data in platform_data["manifest_install_data"].items():
        path = os.path.join(install_data["path"], NAME + ".json")
        exists = os.path.isfile(path)
        status[browser] = exists
    return status
