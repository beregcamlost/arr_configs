#!/usr/bin/env python3
"""intake_webhook.py — Bazarr/Sonarr/Radarr intake-time language detection webhook.

Listens on 127.0.0.1:8765 (override via INTAKE_WEBHOOK_PORT env var).
Accepts POST /bazarr, /sonarr, /radarr with a JSON payload containing a
subtitle file path.  Runs sub_lang_sniff.py on the file and quarantines
(renames to .rejected.srt) any subtitle whose language does not match the
expected language declared in the payload.

Endpoints:
    POST /bazarr   — Bazarr "On Subtitle Download" webhook
    POST /sonarr   — Sonarr post-download webhook (subtitle path field)
    POST /radarr   — Radarr post-download webhook (subtitle path field)
    GET  /health   — liveness probe

Payload fields accepted (first non-empty win):
    subtitle_path, subtitlePath, srt_path, path, subtitle

Expected-language fields accepted (first non-empty win):
    expected_lang, expectedLang, language, lang
    If none present, defaults to env var INTAKE_WEBHOOK_DEFAULT_LANG (default: "es").

Exit codes from sub_lang_sniff.py:
    0  OK (MATCH)
    1  WRONG_LANG  -> quarantine
    2  UNCERTAIN / EMPTY -> pass through
    3  IO error -> log + pass through (file may have moved)

Usage:
    python3 intake_webhook.py          # starts server
    INTAKE_WEBHOOK_PORT=9000 python3 intake_webhook.py
"""

import http.server
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http import HTTPStatus
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HOST = "127.0.0.1"
PORT = int(os.environ.get("INTAKE_WEBHOOK_PORT", "8765"))
VERSION = "1.0"
SCRIPT_DIR = Path(__file__).resolve().parent
SNIFF_SCRIPT = SCRIPT_DIR / "sub_lang_sniff.py"
LOG_DIR = Path("/config/berenstuff/automation/logs")
LOG_FILE = LOG_DIR / "intake_webhook.log"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DEFAULT_LANG = os.environ.get("INTAKE_WEBHOOK_DEFAULT_LANG", "es")

# Subtitle path field names to check in payload (first match wins)
PATH_FIELDS = ("subtitle_path", "subtitlePath", "srt_path", "path", "subtitle")
# Language field names to check in payload (first match wins)
LANG_FIELDS = ("expected_lang", "expectedLang", "language", "lang")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_DIR.mkdir(parents=True, exist_ok=True)

_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
try:
    _handlers.append(logging.FileHandler(LOG_FILE))
except OSError as _e:
    print(f"[intake_webhook] WARNING: cannot open log file {LOG_FILE}: {_e}", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [intake_webhook] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger("intake_webhook")

# ---------------------------------------------------------------------------
# Startup time for uptime calculation
# ---------------------------------------------------------------------------
START_TIME = time.monotonic()

# ---------------------------------------------------------------------------
# Graceful shutdown flag
# ---------------------------------------------------------------------------
_shutdown = threading.Event()


def _sigterm_handler(signum: int, frame: object) -> None:
    log.info("Received signal %d — initiating graceful shutdown", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT, _sigterm_handler)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _extract_field(payload: dict, candidates: tuple[str, ...]) -> str:
    """Return the first non-empty string value found among candidate keys."""
    for key in candidates:
        val = payload.get(key)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _run_sniff(srt_path: str, expected_lang: str) -> tuple[int, str]:
    """Run sub_lang_sniff.py.  Returns (exit_code, stdout_line)."""
    try:
        result = subprocess.run(
            [sys.executable, str(SNIFF_SCRIPT), srt_path, expected_lang],
            capture_output=True,
            text=True,
            timeout=60,
        )
        stdout = result.stdout.strip()
        return result.returncode, stdout
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except OSError as exc:
        return -1, f"EXEC_ERROR: {exc}"


def _quarantine(srt_path: str) -> str:
    """Rename subtitle to <path>.rejected.srt.  Returns new path."""
    p = Path(srt_path)
    # Build rejected name: strip existing extension, append .rejected.srt
    rejected = p.with_name(p.stem + ".rejected.srt")
    # If target exists, add a numeric suffix to avoid collision
    if rejected.exists():
        i = 1
        while rejected.exists():
            rejected = p.with_name(f"{p.stem}.rejected.{i}.srt")
            i += 1
    p.rename(rejected)
    return str(rejected)


def _discord_alert(message: str) -> None:
    """Post a message to Discord if DISCORD_WEBHOOK_URL is set."""
    if not DISCORD_WEBHOOK_URL:
        return
    payload = json.dumps({"content": message}).encode()
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 204):
                log.warning("Discord returned HTTP %d", resp.status)
    except OSError as exc:
        log.warning("Discord alert failed: %s", exc)


def _handle_subtitle_event(payload: dict, source: str) -> dict:
    """
    Core business logic shared across /bazarr, /sonarr, /radarr.
    Returns a response dict with keys: action, reason (optional), path (optional).
    Raises ValueError with a human-readable message on bad input.
    """
    srt_path = _extract_field(payload, PATH_FIELDS)
    if not srt_path:
        raise ValueError(
            f"No subtitle path found in payload. "
            f"Accepted field names: {', '.join(PATH_FIELDS)}"
        )

    if not Path(srt_path).is_file():
        raise ValueError(f"Subtitle file not found on disk: {srt_path}")

    expected_lang = _extract_field(payload, LANG_FIELDS) or DEFAULT_LANG
    log.info("[%s] Checking %s (expected_lang=%s)", source, srt_path, expected_lang)

    exit_code, sniff_output = _run_sniff(srt_path, expected_lang)

    if exit_code == 1:
        # WRONG_LANG — quarantine
        log.warning("[%s] WRONG_LANG: %s — output: %s", source, srt_path, sniff_output)
        try:
            rejected_path = _quarantine(srt_path)
        except OSError as exc:
            log.error("[%s] Quarantine rename failed for %s: %s", source, srt_path, exc)
            return {"action": "quarantine_failed", "reason": str(exc), "path": srt_path}

        log.warning("[%s] Quarantined -> %s", source, rejected_path)
        alert_msg = (
            f"[intake_webhook] WRONG_LANG detected on intake\n"
            f"Source: {source}\n"
            f"File: {srt_path}\n"
            f"Sniff: {sniff_output}\n"
            f"Quarantined as: {rejected_path}"
        )
        _discord_alert(alert_msg)
        return {
            "action": "rejected",
            "reason": sniff_output,
            "original_path": srt_path,
            "quarantined_as": rejected_path,
        }

    elif exit_code == 0:
        log.info("[%s] MATCH: %s — %s", source, srt_path, sniff_output)
        return {"action": "passed", "verdict": sniff_output}

    elif exit_code == 2:
        log.info("[%s] UNCERTAIN/EMPTY: %s — %s (passing through)", source, srt_path, sniff_output)
        return {"action": "passed", "verdict": sniff_output or "UNCERTAIN"}

    else:
        # exit_code == 3 (IO error) or -1 (timeout/exec error)
        log.warning(
            "[%s] Sniff error (exit %d): %s — passing through", source, exit_code, sniff_output
        )
        return {"action": "passed", "verdict": f"sniff_error(exit={exit_code})"}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class WebhookHandler(http.server.BaseHTTPRequestHandler):
    """Minimal HTTP handler for webhook and health endpoints."""

    # Suppress built-in access logging — we use our own
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ARG002
        pass

    def _send_json(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # GET /health
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 — stdlib naming convention
        if self.path == "/health":
            uptime = int(time.monotonic() - START_TIME)
            self._send_json(HTTPStatus.OK, {"ok": True, "version": VERSION, "uptime_sec": uptime})
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    # ------------------------------------------------------------------
    # POST /bazarr  /sonarr  /radarr
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        route = self.path.rstrip("/")
        if route not in ("/bazarr", "/sonarr", "/radarr"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown route: {self.path}"})
            return

        source = route.lstrip("/")
        payload = self._read_json_body()
        if payload is None:
            log.warning("[%s] Bad JSON body from %s", source, self.client_address[0])
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON body"})
            return

        try:
            result = _handle_subtitle_event(payload, source)
            self._send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            log.warning("[%s] Bad request: %s", source, exc)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] Unhandled error: %s", source, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})


# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

class _ShutdownableServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer that can be stopped via _shutdown event."""

    # Allow rapid restart without address-already-in-use errors
    allow_reuse_address = True

    def service_actions(self) -> None:
        if _shutdown.is_set():
            # Raise SystemExit to break out of serve_forever()
            raise SystemExit(0)


def main() -> None:
    if not SNIFF_SCRIPT.is_file():
        log.error("sub_lang_sniff.py not found at %s — aborting", SNIFF_SCRIPT)
        sys.exit(1)

    server = _ShutdownableServer((HOST, PORT), WebhookHandler)
    log.info(
        "Intake webhook listening on %s:%d (version %s, default_lang=%s)",
        HOST, PORT, VERSION, DEFAULT_LANG,
    )
    log.info("Endpoints: POST /bazarr  /sonarr  /radarr  |  GET /health")
    log.info("Sniff script: %s", SNIFF_SCRIPT)
    log.info("Log file: %s", LOG_FILE)

    try:
        server.serve_forever(poll_interval=0.5)
    except SystemExit:
        pass
    finally:
        server.server_close()
        log.info("Server stopped cleanly")


if __name__ == "__main__":
    main()
