"""enrich + personalize against a local mock of LinkedIn's profile pages and a fake Claude.

The browser tests run a real headless browser and are skipped when Playwright isn't
installed. Nothing here contacts LinkedIn or Claude.
"""

import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import linkedin_browser as lb  # noqa: E402
import outreach  # noqa: E402

try:
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

# The profile page as LinkedIn renders it now: name as an h2, "· 1st" badge, About (truncated
# behind a "…more" button), Activity, and a sidebar of other people. No Experience here.
PROFILE = """<!doctype html><html><body><main>
<section><h2>Jane Doe</h2><div>· 1st</div><div>Partner at Acme Ventures</div><div>Lisbon, Portugal</div>
<div>·</div><a>Contact info</a><div>500+ connections</div><a href="/messaging/compose/?recipient=janedoe">Message</a></section>
<section><h2>About</h2><p>Early-stage investor. Before Acme I ran product at Bloom for six years<span id="rest" hidden>, mostly on payments.</span>
<button id="more">…more</button></p></section>
<section><h2>Activity</h2><p>1,200 followers</p><p>Jane Doe reposted this</p></section>
<aside><h3>More profiles for you</h3><p>Someone Else</p><p>· 2nd</p></aside>
</main><script>
document.getElementById('more').onclick = () => { document.getElementById('rest').hidden = false; document.getElementById('more').remove(); };
</script></body></html>"""

EXPERIENCE = """<!doctype html><html><body><main>
<h2>Experience</h2>
<ul><li><p>Partner</p><p>Acme Ventures · Full-time</p><p>Jan 2022 - Present · 4 yrs</p><p>Seed and Series A in fintech.</p></li>
<li><p>Head of Product</p><p>Bloom</p><p>2016 - 2022 · 6 yrs</p></li></ul>
<aside><h3>More profiles for you</h3><p>Someone Else</p></aside>
</main></body></html>"""

EDUCATION = """<!doctype html><html><body><main>
<h2>Education</h2>
<ul><li><p>University of Lisbon</p><p>MSc, Computer Science</p><p>2010 – 2012</p></li></ul>
<aside><h3>More profiles for you</h3><p>Someone Else</p></aside>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if "/details/experience" in self.path:
            html = EXPERIENCE
        elif "/details/education" in self.path:
            html = EDUCATION
        elif self.path.startswith("/in/"):
            html = PROFILE
        else:
            html = "<html><body><main>feed</main></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")  # LinkedIn's "…more" is UTF-8
        self.end_headers()
        self.wfile.write(html.encode())


class FakeClaude:
    name = "a fake Claude"

    def __init__(self, reply=None, fail=False, replies=None):
        self.prompts = []
        self.reply = reply
        self.fail = fail
        self.replies = list(replies or [])

    def generate(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("quota")
        return self.replies.pop(0) if self.replies else self.reply


class PromptTest(unittest.TestCase):
    def test_prompt_includes_profile_and_rules(self):
        contact = {"first_name": "Jane", "last_name": "Doe", "position": "Partner", "company": "Acme"}
        profile = {"headline": "Partner at Acme Ventures", "location": "Lisbon", "about": "Early-stage investor.",
                   "experience": "Partner\nAcme Ventures", "education": "University of Lisbon"}
        p = outreach.personalization_prompt("Hi Jane,\n\nDraft.", contact, "Get feedback", profile)
        for needle in ("Early-stage investor.", "University of Lisbon", "Get feedback", "Hi Jane,", "Never invent"):
            self.assertIn(needle, p)
        self.assertIn("unknown", outreach.personalization_prompt("x", {**contact, "company": ""}, "g", None))

    def test_over_long_result_gets_one_cut(self):
        contact = {"first_name": "Jane", "last_name": "Doe", "position": "", "company": ""}
        base = "Hi Jane, " + "word " * 20
        long = "Hi Jane, " + "word " * 40
        ai = FakeClaude(replies=[long, "Hi Jane, short."])
        self.assertEqual(outreach.personalize(ai, base, contact, "g"), "Hi Jane, short.")
        self.assertEqual(len(ai.prompts), 2)
        self.assertIn("the limit is", ai.prompts[1])
        ai = FakeClaude(replies=[long, long + "more"])  # the cut didn't help: keep the shorter one
        self.assertEqual(outreach.personalize(ai, base, contact, "g"), long.strip())
        ai = FakeClaude(reply="Hi Jane, fine.")  # within the limit: one call only
        outreach.personalize(ai, base, contact, "g")
        self.assertEqual(len(ai.prompts), 1)


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class EnrichPersonalizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        outreach.main(["--db", self.dbpath, "draft", "c", "--template", str(HERE / "templates/product-beta.txt"),
                       "--sender-name", "Sam", "--var", "product=P", "--var", "audience=A", "--var", "problem=Q",
                       "--limit", "2"])
        self.db = outreach.connect(self.dbpath)
        for r in self.db.execute("SELECT id, url FROM contacts"):  # point everyone at the mock
            self.db.execute("UPDATE contacts SET url=? WHERE id=?", (self.base + "/in/" + r["url"].rsplit("/", 1)[-1], r["id"]))
        self.db.commit()
        self.page_factory = lambda: lb.browser(str(Path(self.tmp.name) / "profile"), headless=True)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def args(self, cmd, *extra):
        return outreach.build_parser().parse_args(["--db", self.dbpath, cmd, *extra, "--min-delay", "0", "--max-delay", "0",
                                                   "--profile-dir", str(Path(self.tmp.name) / "profile")])

    def test_read_profile_expands_and_splits_sections(self):
        with self.page_factory() as page:
            p = lb.read_profile(page, self.base + "/in/janedoe", sleep=lambda s: None, pause=(0, 0))
        self.assertEqual(p["headline"], "Partner at Acme Ventures")
        self.assertEqual(p["location"], "Lisbon, Portugal")
        self.assertIn("mostly on payments", p["about"])  # the "…more" text was expanded
        self.assertNotIn("…more", p["about"])
        self.assertNotIn("followers", p["about"])  # stops at the next heading
        self.assertIn("Head of Product", p["experience"])
        self.assertNotIn("Someone Else", p["experience"])  # stops at the sidebar
        self.assertIn("University of Lisbon", p["education"])

    def test_enrich_saves_and_skips_recent(self):
        n = outreach.cmd_enrich(self.db, self.args("enrich", "--campaign", "c"), page_factory=self.page_factory,
                                sleep=lambda s: None)
        self.assertEqual(n, 2)
        rows = self.db.execute("SELECT * FROM profiles").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn("Acme Ventures", rows[0]["experience"])
        n = outreach.cmd_enrich(self.db, self.args("enrich", "--campaign", "c"), page_factory=self.page_factory,
                                sleep=lambda s: None)
        self.assertEqual(n, 0)  # already read

    def test_personalize_rewrites_waiting_drafts_and_keeps_template_on_failure(self):
        ai = FakeClaude(reply="Hi Jane,\n\nPersonal version.\n\nSam")
        outreach.cmd_personalize(self.db, self.args("personalize", "c", "--brief", "Get feedback", "--enrich"),
                                 ai=ai, page_factory=self.page_factory, sleep=lambda s: None)
        msgs = outreach.campaign_messages(self.db, "c", "draft")
        self.assertEqual([m["body"] for m in msgs], ["Hi Jane,\n\nPersonal version.\n\nSam"] * 2)
        self.assertTrue(all(m["ai_at"] for m in msgs))
        self.assertEqual(len(ai.prompts), 2)
        self.assertIn("Early-stage investor", ai.prompts[0])  # the enriched profile reached Claude
        self.assertIn("Get feedback", ai.prompts[0])
        self.assertEqual(self.db.execute("SELECT brief FROM campaigns").fetchone()[0], "Get feedback")

        # Already personalized: nothing happens without --redo
        ai2 = FakeClaude(reply="again")
        outreach.cmd_personalize(self.db, self.args("personalize", "c"), ai=ai2)
        self.assertEqual(ai2.prompts, [])

        # --redo starts from the template text again, not from the last rewrite
        ai3 = FakeClaude(reply="Hi Jane,\n\nAgain.\n\nSam")
        outreach.cmd_personalize(self.db, self.args("personalize", "c", "--redo"), ai=ai3)
        self.assertIn("Hope all is well at", ai3.prompts[0])  # the template text
        self.assertNotIn("Personal version", ai3.prompts[0])

        # A failing Claude keeps the existing text
        before = [m["body"] for m in outreach.campaign_messages(self.db, "c", "draft")]
        outreach.cmd_personalize(self.db, self.args("personalize", "c", "--redo"), ai=FakeClaude(fail=True))
        self.assertEqual([m["body"] for m in outreach.campaign_messages(self.db, "c", "draft")], before)

    def test_personalize_needs_a_brief(self):
        with self.assertRaises(SystemExit):
            outreach.cmd_personalize(self.db, self.args("personalize", "c"), ai=FakeClaude(reply="x"))


if __name__ == "__main__":
    unittest.main()
