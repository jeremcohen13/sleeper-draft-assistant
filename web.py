#!/usr/bin/env python3
"""Web dashboard for the draft assistant.

    python web.py                 # follow the real draft at http://localhost:8765
    python web.py --dry-run       # rehearse; make your picks from the page

Runs the same polling/recommendation logic as draft.py in a background thread
and serves a single-page UI plus a small JSON API (stdlib only, no extra deps).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from draft_assistant.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from draft_assistant.session import DraftSession, SessionError, build_session
from draft_assistant.sleeper import SleeperAPIError

STATIC_DIR = Path(__file__).resolve().parent / "draft_assistant" / "web"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI flags."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dry-run", action="store_true", help="Simulate the draft with CPU opponents; pick from the page")
    ap.add_argument("--auto", action="store_true", help="(dry-run) auto-accept the TAKE recommendation for your picks")
    ap.add_argument("--slot", type=int, default=0, help="(dry-run) draft slot if you are not in the draft order")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--sim-delay", type=float, default=1.5, help="(dry-run) seconds between simulated picks")
    ap.add_argument("--poll", type=float, default=None, help="Override poll interval in seconds")
    ap.add_argument("--rankings", default=None)
    ap.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically")
    return ap.parse_args(argv)


def build_logger(logs_path: Path, verbosity: str) -> logging.Logger:
    """File logger shared with draft.py's format."""
    from datetime import date

    logs_path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("draft")
    logger.setLevel(logging.DEBUG if verbosity == "debug" else logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(logs_path / f"draft_{date.today().isoformat()}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger


class Poller(threading.Thread):
    """Background thread: poll, recompute the snapshot, auto-pick in dry-run if asked."""

    def __init__(self, session: DraftSession, interval: float, auto: bool) -> None:
        super().__init__(daemon=True, name="poller")
        self.session = session
        self.interval = interval
        self.auto = auto
        self.snapshot: dict[str, Any] = {"status": "starting", "picks": [], "available": [], "rec": None, "starters": [], "bench": []}
        self.stop_event = threading.Event()
        self._last_rec_key: tuple[int | None, bool] | None = None

    def refresh(self) -> None:
        """Recompute the JSON snapshot (called after every poll and every pick)."""
        rec = self.session.recommendation()
        st = self.session.state
        key = (st.current_pick_no, st.is_my_turn)
        if self.session.status in ("drafting", "paused") and (st.is_my_turn or st.is_on_deck) and key != self._last_rec_key:
            self._last_rec_key = key
            self.session.log_recommendation(rec)
        snap = self.session.snapshot(rec)
        snap["poll_interval"] = self.interval
        self.snapshot = snap

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.session.poll()
            except SleeperAPIError:
                self.snapshot = {**self.snapshot, "error": self.session.last_error, "polls": self.session.polls}
                self.stop_event.wait(self.interval)
                continue
            self.refresh()
            st = self.session.state
            if self.auto and self.session.simulator is not None and self.session.status == "drafting" and st.is_my_turn:
                take = self.snapshot.get("rec", {}).get("take")
                if take and take.get("id"):
                    try:
                        self.session.submit_pick(take["id"])
                    except ValueError as exc:
                        self.session.logger.warning("auto pick failed: %s", exc)
                    self.refresh()
                    continue
            if self.session.status == "complete" or st.is_complete:
                self.session.logger.info("draft complete; roster: %s", "; ".join(f"{p.name} {p.position}" for p in st.my_roster))
                self.stop_event.wait(self.interval * 5)
                continue
            self.stop_event.wait(self.interval)


def make_handler(poller: Poller) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to a poller."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "draft-assistant/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            poller.session.logger.debug("http %s", format % args)

        def _send(self, status: HTTPStatus, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                page = (STATIC_DIR / "index.html").read_bytes()
                self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(HTTPStatus.OK, poller.snapshot)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if path == "/api/pick":
                pid = str(body.get("id") or "")
                try:
                    pick = poller.session.submit_pick(pid)
                except ValueError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                    return
                poller.refresh()
                self._json(HTTPStatus.OK, {"ok": True, "pick": poller.session.pick_json(pick)})
            elif path == "/api/pin":
                try:
                    poller.session.set_pin(str(body.get("id") or ""), bool(body.get("pinned", True)))
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                poller.refresh()
                self._json(HTTPStatus.OK, {"ok": True, "targets": poller.snapshot.get("targets", [])})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return Handler


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    logger = build_logger(cfg.logs_path, cfg.verbosity)
    logger.info("=== web.py start dry_run=%s auto=%s ===", args.dry_run, args.auto)
    try:
        session = build_session(cfg, logger, dry_run=args.dry_run, slot_override=args.slot, seed=args.seed, rankings_override=args.rankings)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2 if "slot is unknown" in str(exc) else 1

    interval = args.sim_delay if args.dry_run else (args.poll or cfg.poll_interval)
    poller = Poller(session, interval, auto=args.auto)
    poller.start()
    server = None
    port = args.port
    for candidate in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer((args.host, candidate), make_handler(poller))
            port = candidate
            break
        except OSError as exc:
            if getattr(exc, "errno", None) not in (48, 98):  # EADDRINUSE on macOS / Linux
                raise
            print(f"port {candidate} is in use (another web.py still running?), trying {candidate + 1}")
    if server is None:
        print(f"error: no free port between {args.port} and {args.port + 9}; stop the other web.py or pass --port", file=sys.stderr)
        poller.stop_event.set()
        return 1
    url = f"http://{args.host}:{port}/"
    print(f"{session.league.get('name')} — slot {session.my_slot} — {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"Dashboard: {url}   (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        poller.stop_event.set()
        server.server_close()
        logger.info("web.py stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
