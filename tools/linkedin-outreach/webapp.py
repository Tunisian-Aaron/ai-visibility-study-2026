#!/usr/bin/env python3
"""Local web interface for the LinkedIn outreach tool.

    python webapp.py            # opens http://127.0.0.1:8765 in your browser

Everything runs on your computer: the page talks to this small server, which
uses the same database and code as outreach.py. It only listens on 127.0.0.1
and rejects requests from other websites.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import outreach  # noqa: E402

MAX_BODY = 50 * 1024 * 1024  # Connections.csv for ~30k connections is a few MB


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --- per-thread stdout capture ------------------------------------------------
# outreach.py reports progress with print(); route each worker thread's output
# into its own buffer so the page can show it.

class _StdoutRouter(io.TextIOBase):
    def __init__(self, original):
        self.original = original
        self.targets: dict[int, list] = {}

    def write(self, s):
        target = self.targets.get(threading.get_ident())
        if target is not None:
            target.append(s)
        else:
            self.original.write(s)
        return len(s)

    def flush(self):
        self.original.flush()


ROUTER = _StdoutRouter(sys.stdout)


def captured(fn):
    """Run fn() in this thread, returning what it printed. SystemExit becomes an ApiError."""
    buf: list = []
    ROUTER.targets[threading.get_ident()] = buf
    try:
        fn()
    except SystemExit as e:
        raise ApiError(str(e) or "failed")
    finally:
        ROUTER.targets.pop(threading.get_ident(), None)
    return "".join(buf).strip()


# --- background jobs (login, autosend) ------------------------------------------

class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind: str):
        self.kind = kind
        self.log: list = []
        self.running = True
        self.stop = threading.Event()
        self.login_done = threading.Event()
        self.thread: threading.Thread | None = None

    def text(self, limit=40000):
        return "".join(self.log)[-limit:]


class App:
    def __init__(self, db_path: str, profile_dir: str | None, channel: str | None):
        import linkedin_browser as lb
        self.db_path = db_path
        self.profile_dir = profile_dir or lb.DEFAULT_PROFILE_DIR
        self.channel = channel
        self.job: Job | None = None
        self.lock = threading.Lock()

    def db(self):
        return outreach.connect(self.db_path)

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    def _list(value):
        if isinstance(value, list):
            return [v.strip() for v in value if str(v).strip()]
        return [v.strip() for v in str(value or "").split(",") if v.strip()]

    def filters(self, f: dict, **extra):
        f = f or {}
        ns = argparse.Namespace(
            company=self._list(f.get("company")), title=self._list(f.get("title")),
            exclude_company=self._list(f.get("exclude_company")),
            exclude_title=self._list(f.get("exclude_title")),
            since=f.get("since") or None, before=f.get("before") or None,
            ids=f.get("ids") or None, limit=int(f["limit"]) if f.get("limit") else None)
        for k, v in extra.items():
            setattr(ns, k, v)
        return ns

    def draft_ns(self, b: dict, dry_run: bool):
        campaign = (b.get("campaign") or "").strip()
        if not campaign:
            raise ApiError("Give the campaign a name.")
        if not (b.get("template") or "").strip():
            raise ApiError("The message template is empty.")
        return self.filters(
            b.get("filters"), campaign=campaign, template=None, template_text=b["template"],
            brief=None, ai=False, dry_run=dry_run, sender_name=b.get("sender_name") or "",
            var=[f"{k}={v}" for k, v in (b.get("vars") or {}).items()],
            cooldown_days=int(b.get("cooldown_days") or 30))

    def start_job(self, kind, target):
        with self.lock:
            if self.job and self.job.running:
                raise ApiError(f"Already running: {self.job.kind}. Stop it first.", 409)
            job = self.job = Job(kind)

        def run():
            ROUTER.targets[threading.get_ident()] = job.log
            try:
                target(job)
            except Cancelled:
                job.log.append("\nStopped by you.\n")
            except SystemExit as e:
                job.log.append(f"\n{e}\n")
            except Exception as e:
                first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
                hint = ""
                if "Executable doesn't exist" in str(e):
                    hint = "\nRun: playwright install chromium\n"
                elif "Missing X server" in str(e) or "$DISPLAY" in str(e):
                    hint = "\nThis computer has no display - tick 'Hide the browser window' and try again.\n"
                job.log.append(f"\nError: {first}{hint}")
            finally:
                ROUTER.targets.pop(threading.get_ident(), None)
                job.running = False

        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    # -- endpoints --------------------------------------------------------------
    def api_state(self, b):
        db = self.db()
        camps = []
        for k in db.execute("SELECT * FROM campaigns ORDER BY id DESC"):
            counts = dict(db.execute("SELECT status, COUNT(*) FROM messages WHERE campaign_id=? "
                                     "GROUP BY status", (k["id"],)).fetchall())
            camps.append({"name": k["name"], "template": k["template"],
                          "counts": {s: counts.get(s, 0) for s in outreach.STATUSES}})
        templates = {p.stem: p.read_text(encoding="utf-8")
                     for p in sorted((HERE / "templates").glob("*.txt"))}
        job = self.job
        return {
            "contacts": db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
            "do_not_contact": db.execute("SELECT COUNT(*) FROM contacts WHERE do_not_contact=1").fetchone()[0],
            "sent_24h": outreach.sent_last_24h(db),
            "campaigns": camps,
            "templates": templates,
            "logged_in_before": Path(self.profile_dir).exists(),
            "job": {"kind": job.kind, "running": job.running, "log": job.text()} if job else None,
        }

    def api_import(self, b):
        text = b.get("csv_text") or ""
        if not text.strip():
            raise ApiError("That file is empty.")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        try:
            out = captured(lambda: outreach.cmd_import(self.db(), argparse.Namespace(csv=path)))
        finally:
            Path(path).unlink(missing_ok=True)
        return {"message": out}

    def api_contacts(self, b):
        rows = outreach.select_contacts(self.db(), self.filters(b.get("filters")))
        return {"contacts": [{k: r[k] for k in ("id", "first_name", "last_name", "position", "company",
                                                 "connected_on", "url")} for r in rows]}

    def api_preview(self, b):
        db, ns = self.db(), self.draft_ns(b, dry_run=True)
        contacts = outreach.draft_candidates(db, ns)
        sender = outreach.sender_values(ns)
        # Your own fields without a {x|fallback} must be filled, or messages go out with gaps.
        blanks = sorted({m.group(1) for m in outreach.PLACEHOLDER.finditer(ns.template_text)
                         if m.group(1).startswith("sender_") and m.group(2) is None
                         and not sender.get(m.group(1).removeprefix("sender_"), "").strip()})
        if blanks:
            names = ", ".join("your name" if f == "sender_name" else "{%s}" % f for f in blanks)
            raise ApiError(f"Fill in {names} first - otherwise the message would have gaps.")
        previews = []
        for c in contacts[:3]:
            try:
                body = outreach.render(ns.template_text, c, sender)
            except KeyError as e:
                raise ApiError(f"{e.args[0]} - fill in that field or remove it from the template.")
            previews.append({"name": f"{c['first_name']} {c['last_name']}",
                             "subtitle": " @ ".join(x for x in (c["position"], c["company"]) if x),
                             "body": body})
        return {"count": len(contacts), "previews": previews}

    def api_draft(self, b):
        self.api_preview(b)  # validates placeholders before anything is saved
        out = captured(lambda: outreach.cmd_draft(self.db(), self.draft_ns(b, dry_run=False)))
        return {"message": out}

    def api_messages(self, b):
        rows = outreach.campaign_messages(self.db(), b.get("campaign") or "", b.get("status") or None)
        return {"messages": [{k: r[k] for k in ("id", "contact_id", "first_name", "last_name", "url",
                                                 "company", "position", "status", "body", "error",
                                                 "sent_at")} for r in rows]}

    def api_update_message(self, b):
        db = self.db()
        mid = int(b["id"])
        if "body" in b:
            body = (b["body"] or "").strip()
            if not body:
                raise ApiError("A message can't be empty.")
            db.execute("UPDATE messages SET body=?, updated_at=? WHERE id=?", (body, outreach.now(), mid))
        if "status" in b:
            if b["status"] not in outreach.STATUSES:
                raise ApiError("Unknown status.")
            db.execute("UPDATE messages SET status=?, error=CASE WHEN ?='draft' THEN NULL ELSE error END, "
                       "sent_at=CASE WHEN ? IN ('sent','replied') THEN COALESCE(sent_at, ?) ELSE sent_at END, "
                       "updated_at=? WHERE id=?",
                       (b["status"], b["status"], b["status"], outreach.now(), outreach.now(), mid))
        db.commit()
        return {"ok": True}

    def api_dnc(self, b):
        db = self.db()
        db.execute("UPDATE contacts SET do_not_contact=? WHERE id=?", (0 if b.get("undo") else 1, int(b["contact_id"])))
        db.commit()
        return {"ok": True}

    def api_login_start(self, b):
        import linkedin_browser as lb

        def run(job):
            def wait(msg):
                print("A browser window has opened. Log in to LinkedIn there (including any 2FA), "
                      "then click \"I'm logged in\" on this page.")
                job.login_done.wait()
                return ""
            lb.interactive_login(self.profile_dir, channel=self.channel, prompt=wait)

        self.start_job("login", run)
        return {"ok": True}

    def api_login_done(self, b):
        if self.job and self.job.kind == "login":
            self.job.login_done.set()
        return {"ok": True}

    def api_autosend(self, b):
        campaign = b.get("campaign") or ""
        argv = ["--db", self.db_path, "autosend", campaign, "--yes",
                "--daily-cap", str(int(b.get("daily_cap") or 20)),
                "--min-delay", str(float(b.get("min_delay") or 60)),
                "--max-delay", str(float(b.get("max_delay") or 180)),
                "--profile-dir", self.profile_dir,
                "--screenshots", str(HERE / "failures")]
        if b.get("max"):
            argv += ["--max", str(int(b["max"]))]
        if b.get("dry_run"):
            argv.append("--dry-run")
        if b.get("headless"):
            argv.append("--headless")
        if self.channel:
            argv += ["--channel", self.channel]
        ns = outreach.build_parser().parse_args(argv)
        if not campaign:
            raise ApiError("Pick a campaign.")

        def run(job):
            def sleep(seconds):
                if job.stop.wait(seconds):
                    raise Cancelled()
            if job.stop.is_set():
                raise Cancelled()
            outreach.cmd_autosend(outreach.connect(self.db_path), ns, sleep=sleep, prompt=lambda _: "send")

        self.start_job("test run" if b.get("dry_run") else "sending", run)
        return {"ok": True}

    def api_stop(self, b):
        if self.job and self.job.running:
            self.job.stop.set()
            self.job.login_done.set()
            self.job.log.append("\nStopping after the current message...\n")
        return {"ok": True}

    ROUTES = {
        "state": api_state, "import": api_import, "contacts": api_contacts,
        "preview": api_preview, "draft": api_draft, "messages": api_messages,
        "message": api_update_message, "dnc": api_dnc, "login/start": api_login_start,
        "login/done": api_login_done, "autosend": api_autosend, "stop": api_stop,
    }


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    index = (HERE / "web" / "index.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body: bytes, ctype):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _host_ok(self):
            # Blocks DNS-rebinding: only answer requests addressed to this machine.
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"Forbidden", "text/plain")
            if self.path in ("/", "/index.html"):
                return self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            self._send(404, b"Not found", "text/plain")

        def do_POST(self):
            # The custom header can't be sent cross-site without a CORS preflight, which we never
            # approve, so other websites can't drive this API from your browser.
            if not self._host_ok() or self.headers.get("X-Outreach") != "1":
                return self._json(403, {"error": "Forbidden"})
            route = self.path.removeprefix("/api/")
            fn = App.ROUTES.get(route)
            if fn is None:
                return self._json(404, {"error": "Not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "File too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                self._json(200, fn(app, body))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"error": f"Bad request: {e}"})
            except Exception as e:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def main(argv=None):
    p = argparse.ArgumentParser(description="Local web interface for the LinkedIn outreach tool.")
    p.add_argument("--db", default=outreach.DEFAULT_DB)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--profile-dir", help="browser profile for your LinkedIn login")
    p.add_argument("--channel", choices=["chrome", "msedge"], help="use installed Chrome/Edge")
    p.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    args = p.parse_args(argv)

    sys.stdout = ROUTER
    app = App(args.db, args.profile_dir, args.channel)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app, args.port))
    except OSError as e:
        raise SystemExit(f"Can't listen on port {args.port} ({e.strerror}). Is the app already running? "
                         f"Open http://127.0.0.1:{args.port}, or start this one with --port {args.port + 1}.")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Outreach is running at {url}  (Ctrl-C to quit)\nDatabase: {Path(args.db).resolve()}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
