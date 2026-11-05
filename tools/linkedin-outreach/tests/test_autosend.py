"""End-to-end test of autosend against a local mock of LinkedIn's profile + messaging UI.

Runs a real headless browser; skipped when Playwright isn't installed. Set
OUTREACH_BROWSER_PATH if Playwright's bundled Chromium isn't installed.
"""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import outreach  # noqa: E402

try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

# The composer, as on LinkedIn's /messaging/compose page and in the older overlay.
COMPOSER = """<div id="overlay" {hidden}>
  <div class="msg-form__contenteditable" contenteditable="true" role="textbox"
       aria-label="Write a message…" style="min-height:40px;border:1px solid"></div>
  <button class="msg-form__send-button" type="submit">Send</button>
  <button aria-label="Close your conversation with {name}">x</button>
</div>
<script>
const box = document.querySelector('.msg-form__contenteditable');
const msgBtn = document.querySelector('main button');
if (msgBtn) msgBtn.onclick = () => document.getElementById('overlay').hidden = false;
document.querySelector('[aria-label^="Close"]').onclick = () => document.getElementById('overlay').hidden = true;
box.addEventListener('keydown', e => {{ if (e.key === 'Enter') window.enterPressed = true; }});
document.querySelector('.msg-form__send-button').onclick = async () => {{
  if ({stuck}) return;
  await fetch('/sent', {{method: 'POST', body: JSON.stringify({{who: '{slug}', text: box.innerText}})}});
  box.innerText = '';
}};
</script>"""

# A profile. Like the real page: name with a degree badge, the Message control, and a
# "More profiles" sidebar whose other people are 2nd degree.
PROFILE = """<!doctype html><html><body><main>
<section><div><div><h1>{name}</h1><span>· {degree}</span></div>
<p>Some headline</p>{button}</div></section>
<aside><h2>More profiles for you</h2><p>Someone Else · 2nd</p><button>Follow</button></aside>
</main>
{composer}</body></html>"""

COMPOSE_PAGE = """<!doctype html><html><body><main><h2>New message</h2>{composer}</main></body></html>"""

# New layout: Message is a link to the compose page. Old layout: a button opening an overlay.
LINK = '<a href="/messaging/compose/?recipient={slug}&interop=msgOverlay">Message</a>'
BUTTON = '<button aria-label="Message {name}">Message</button>'
PREMIUM = '<a href="/messaging/compose/?recipient={slug}&premium=1" aria-label="Message with Premium">Say hello</a>'

PAGES = {
    "ok1": dict(name="Jane Doe", degree="1st", button=LINK, stuck="false"),
    "ok2": dict(name="Raj Patel", degree="1st", button=BUTTON, stuck="false"),
    "nomsg": dict(name="Maria Gomez", degree="1st", button='<button>Follow</button>', stuck="false"),
    "stuck": dict(name="Tom Lee", degree="1st", button=LINK, stuck="true"),
    "second": dict(name="Ava Second", degree="2nd", button=PREMIUM, stuck="false"),
}


def profile_html(slug):
    p = dict(PAGES[slug], slug=slug)
    p["button"] = p["button"].format(**p)
    # the overlay composer only exists in the old (button) layout
    p["composer"] = COMPOSER.format(hidden="hidden", **p) if p["button"].startswith("<button aria") else ""
    return PROFILE.format(**p)


class Handler(BaseHTTPRequestHandler):
    sent = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        slug = self.path.strip("/").split("/")[-1]
        if self.path.startswith("/messaging/compose/"):
            q = dict(p.split("=") for p in self.path.split("?")[1].split("&"))
            if "premium" in q:
                html = "<html><body><main><h2>InMail</h2><p>Buy Premium to send InMail.</p></main></body></html>"
            else:
                slug = q["recipient"]
                html = COMPOSE_PAGE.format(composer=COMPOSER.format(hidden="", slug=slug, **PAGES[slug]))
        elif slug == "restricted":
            html = "<html><body><main>We've detected unusual activity on your account.</main></body></html>"
        elif slug in PAGES:
            html = profile_html(slug)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Handler.sent.append(json.loads(body))
        self.send_response(204)
        self.end_headers()


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class AutosendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/in/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.sent.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        outreach.main(["--db", self.dbpath, "draft", "c", "--template", str(HERE / "templates/product-beta.txt"),
                       "--sender-name", "Sam", "--var", "product=P", "--var", "audience=A", "--var", "problem=Q"])

    def tearDown(self):
        self.tmp.cleanup()

    def point_contacts(self, slugs):
        """Map contacts (in queue order) to mock pages; extra contacts get do-not-contact."""
        db = outreach.connect(self.dbpath)
        ids = [r["contact_id"] for r in outreach.campaign_messages(db, "c", "draft")]
        for cid, slug in zip(ids, slugs):
            db.execute("UPDATE contacts SET url=? WHERE id=?", (self.base + slug, cid))
        for cid in ids[len(slugs):]:
            db.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (cid,))
        db.commit()
        return db, ids

    def autosend(self, db, *extra):
        args = outreach.build_parser().parse_args(
            ["--db", self.dbpath, "autosend", "c", "--headless", "--yes", "--min-delay", "0",
             "--max-delay", "0", "--profile-dir", str(Path(self.tmp.name) / "profile"),
             "--screenshots", str(Path(self.tmp.name) / "shots"), *extra])
        outreach.cmd_autosend(db, args, sleep=lambda s: None)

    def statuses(self, db):
        return {r["contact_id"]: r["status"] for r in db.execute("SELECT contact_id, status FROM messages")}

    def test_sends_and_records(self):
        # ok1: Message is a link to the compose page (current layout); ok2: a button + overlay (old);
        # nomsg: no Message control; second: a 2nd-degree profile whose only link is paid InMail.
        db, ids = self.point_contacts(["ok1", "nomsg", "ok2", "second"])
        self.autosend(db)
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1", "ok2"])
        self.assertIn("Hi ", Handler.sent[0]["text"])
        # multi-line body arrives exactly as drafted, in one message
        expected = outreach.campaign_messages(db, "c", "sent")[0]["body"]
        self.assertEqual(Handler.sent[0]["text"], expected)
        st = self.statuses(db)
        self.assertEqual([st[i] for i in ids[:4]], ["sent", "failed", "sent", "failed"])
        errs = {cid: db.execute("SELECT error FROM messages WHERE contact_id=?", (cid,)).fetchone()[0] for cid in ids[:4]}
        self.assertIn("Message button", errs[ids[1]])
        self.assertIn("1st-degree", errs[ids[3]])
        self.assertTrue(any(Path(self.tmp.name, "shots").glob("failed-*.png")))

    def test_unconfirmed_send_is_not_retried(self):
        db, ids = self.point_contacts(["stuck"])
        import linkedin_browser
        orig = linkedin_browser.LinkedInSender.__init__
        linkedin_browser.LinkedInSender.__init__ = lambda s, p, **k: orig(s, p, timeout_s=5, confirm_timeout_s=1)
        try:
            self.autosend(db)
        finally:
            linkedin_browser.LinkedInSender.__init__ = orig
        self.assertEqual(self.statuses(db)[ids[0]], "failed")

    def test_stops_on_security_notice(self):
        db, ids = self.point_contacts(["ok1", "restricted", "ok2"])
        self.autosend(db)
        self.assertEqual([s["who"] for s in Handler.sent], ["ok1"])
        st = self.statuses(db)
        self.assertEqual([st[i] for i in ids[:3]], ["sent", "draft", "draft"])

    def test_dry_run_sends_nothing(self):
        db, ids = self.point_contacts(["ok1", "ok2"])
        self.autosend(db, "--dry-run")
        self.assertEqual(Handler.sent, [])
        self.assertEqual(set(self.statuses(db)[i] for i in ids[:2]), {"draft"})

    def test_daily_cap(self):
        db, ids = self.point_contacts(["ok1", "ok2"])
        self.autosend(db, "--daily-cap", "1")
        self.assertEqual(len(Handler.sent), 1)
        self.autosend(db, "--daily-cap", "1")  # cap already used: sends nothing
        self.assertEqual(len(Handler.sent), 1)


if __name__ == "__main__":
    unittest.main()
