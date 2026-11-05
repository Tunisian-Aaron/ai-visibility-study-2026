"""import-linkedin against a local mock of LinkedIn's connections page.

The browser test runs a real headless browser and is skipped when Playwright
isn't installed. It never contacts LinkedIn.
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

PEOPLE = [
    ("janedoe-example", "Jane Doe", "Partner at Acme Ventures", "January 15, 2024"),
    ("newperson", "Ana Silva, PhD", "Head of Growth at Bloom | ex-Google", "October 3, 2025"),
    ("nocompany", "Lee Kim", "Founder · Building something new", "March 1, 2023"),
] + [(f"p{i}", f"Person {i}", f"Engineer at Co{i}", "May 2, 2022") for i in range(12)]

# Mimics the real page: an image link and a name link per card, a Message button,
# visually-hidden helper text, 5 cards per batch loaded on scroll, then a
# "Show more results" button for the rest.
PAGE = """<!doctype html><html><body><header><a href="/in/me/">Me</a></header><main><h1>%d Connections</h1><ul id="list"></ul>
<button id="more" hidden>Show more results</button></main>
<script>
const people = %s; let shown = 0;
function add(n) {
  for (const [slug, name, headline, date] of people.slice(shown, shown + n)) {
    const li = document.createElement('li');
    li.style.height = '300px';
    li.innerHTML = `<div><a href="/in/${slug}/?miniProfileUrn=x"><img alt=""></a></div>
      <div><a href="/in/${slug}/"><span>${name}</span></a>
      <span style="position:absolute;clip:rect(0 0 0 0)">View ${name}'s profile</span>
      <p>${headline}</p><time>Connected on ${date}</time></div>
      <button>Message</button>`;
    document.getElementById('list').appendChild(li);
  }
  shown += n;
}
setTimeout(() => add(5), 300);  // rendered after load, like LinkedIn
let loading = false;
window.addEventListener('scroll', () => {
  if (loading || shown >= 10 || innerHeight + scrollY < document.body.scrollHeight - 50) return;
  loading = true; setTimeout(() => { add(5); loading = false;
    if (shown >= 10) document.getElementById('more').hidden = false; }, 200);
});
document.getElementById('more').onclick = () => { add(100); document.getElementById('more').hidden = true; };
</script></body></html>"""


# A login wall that "logs in" by itself after a moment, as if the user typed a password.
LOGIN = """<html><body><main>Sign in</main><script>
setTimeout(() => { location.href = '/dologin'; }, 700);
</script></body></html>"""


# The connections page as it is now: the first few cards in the HTML, the rest fetched 10 at a
# time through a "pagination" POST whose response is a React Server Components stream.
PAGER_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body><main><h1>%d Connections</h1>
<ul>%s</ul><div style="height:3000px"></div></main><script>
let asked = false;
window.addEventListener('scroll', () => {
  if (asked) return; asked = true;
  fetch('/flagship-web/rsc-action/actions/pagination?sduiid=com.linkedin.sdui.pagers.mynetwork.connectionsList&parentSpanId=x',
        {method: 'POST', headers: {'content-type': 'application/json', 'csrf-token': 'ajax:1'},
         body: JSON.stringify({clientArguments: {payload: {startIndex: 3}}, paginationRequest: {requestedArguments: {payload: {startIndex: 3}}}})});
});
</script></body></html>"""


def rsc_person(slug, name, headline):
    """One person as they appear in the stream: a name/headline/slug triple (twice, as on the real page)."""
    import json
    n, h = json.dumps(name)[1:-1], json.dumps(headline)[1:-1]
    one = ('{"id":"profile_name_loading_state"}},"namespace":"LoadingNamespace"},"value":{"$case":"stringValue",'
           f'"stringValue":"{n}"' + '}}},{"id":"profile_headline_loading_state"}},"namespace":"LoadingNamespace"},'
           f'"value":{{"$case":"stringValue","stringValue":"{h}"' + '}}},' + f'"url":"/in/{slug}/","vanityName":"{slug}",')
    return one * 2


def rsc_response(people):
    dates = "".join(f'"children":["Connected on {d}"]' for _, _, _, d in people)
    return "1:I[]\n" + dates + "".join(rsc_person(s, n, h) for s, n, h, _ in people) + "\n"


class Handler(BaseHTTPRequestHandler):
    logged_in = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        import json
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        start = body["clientArguments"]["payload"]["startIndex"]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(rsc_response(PEOPLE[start:start + 10]).encode())

    def do_GET(self):
        if self.path.startswith("/pager"):
            cards = "".join(f'<li><a href="/in/{s}/">{n}</a><p>{h}</p><time>Connected on {d}</time></li>' for s, n, h, d in PEOPLE[:3])
            html = PAGER_PAGE % (len(PEOPLE), cards)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())
            return
        if self.path.startswith("/dologin"):  # the "password accepted" step
            Handler.logged_in = True
            self.send_response(302)
            self.send_header("Location", "/mynetwork/")
            self.end_headers()
            return
        if self.path.startswith("/restricted"):
            html = "<html><body><main>Quick security check</main></body></html>"
        elif self.path.startswith("/login") or self.path.startswith("/authwall"):
            if Handler.logged_in:  # like LinkedIn: /login redirects to the feed once you're in
                self.send_response(302)
                self.send_header("Location", "/feed/")
                self.end_headers()
                return
            html = LOGIN
        else:
            import json
            html = PAGE % (len(PEOPLE), json.dumps(PEOPLE))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


class ParseTest(unittest.TestCase):
    def test_parse_pager_response(self):
        people = [("ana-silva", 'Ana "Nina" Silva, PhD', "Head of Growth at Bloom — ex-Google", "October 3, 2025"),
                  ("lee", "Lee Kim", "Founder", "March 1, 2023")]
        out = lb.parse_pager_response(rsc_response(people))
        self.assertEqual(len(out), 2)  # each person appears twice in the stream, once in the result
        self.assertEqual(out[0]["url"], "https://www.linkedin.com/in/ana-silva")
        self.assertEqual((out[0]["first_name"], out[0]["last_name"]), ("Ana", '"Nina" Silva'))
        self.assertEqual((out[0]["position"], out[0]["company"]), ("Head of Growth", "Bloom — ex-Google"))
        self.assertEqual(out[0]["connected_on"], "October 3, 2025")
        self.assertEqual((out[1]["first_name"], out[1]["connected_on"]), ("Lee", "March 1, 2023"))
        self.assertEqual(lb.parse_pager_response("1:I[]\n"), [])

    def test_split_headline(self):
        self.assertEqual(lb.split_headline("Head of Growth at Bloom | ex-Google"), ("Head of Growth", "Bloom"))
        self.assertEqual(lb.split_headline("CTO @ Acme"), ("CTO", "Acme"))
        self.assertEqual(lb.split_headline("Venture Partner@NetworkVC | VC"), ("Venture Partner", "NetworkVC"))
        self.assertEqual(lb.split_headline("Founder · Building"), ("Founder · Building", ""))

    def test_parse_card_skips_noise(self):
        c = lb.parse_card({"url": "u", "name": "", "lines": [
            "View Jane Doe's profile", "Jane Doe", "Jane Doe", "Partner at Acme", "Connected on May 2, 2022", "Message"]})
        self.assertEqual((c["first_name"], c["last_name"], c["position"], c["company"], c["connected_on"]),
                         ("Jane", "Doe", "Partner", "Acme", "May 2, 2022"))


@unittest.skipUnless(HAVE_PW, "playwright not installed")
class ImportLinkedInTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Handler.logged_in = False
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        self.profile = str(Path(self.tmp.name) / "profile")

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, path="/mynetwork/", *extra, should_stop=lambda: False):
        args = outreach.build_parser().parse_args(
            ["--db", self.dbpath, "import-linkedin", "--headless", "--url", self.base + path,
             "--profile-dir", self.profile, *extra])
        db = outreach.connect(self.dbpath)
        # The mock serves profile links on its own origin; map them to linkedin.com like the real page.
        orig = lb.collect_connections

        def collect(page, on_batch, **kw):
            def remap(batch):
                for c in batch:
                    c["url"] = c["url"].replace(self.base, "https://www.linkedin.com")
                on_batch(batch)
            return orig(page, pause=(0.1, 0.2), on_batch=remap, stall_s=6, pager_timeout_s=3, **kw)
        lb.collect_connections = collect
        try:
            outreach.cmd_import_linkedin(db, args, should_stop=should_stop)
        finally:
            lb.collect_connections = orig
        return db

    def count(self, db):
        return db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]

    def test_scrolls_everything_and_merges_with_csv(self):
        outreach.main(["--db", self.dbpath, "import", str(HERE / "sample_connections.csv")])
        before = outreach.connect(self.dbpath).execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        db = self.run_import()
        rows = {r["url"]: r for r in db.execute("SELECT * FROM contacts")}
        self.assertNotIn("https://www.linkedin.com/in/me", rows)  # header link isn't a connection
        self.assertEqual(len(rows), before + len(PEOPLE) - 1)  # jane was already in the CSV
        ana = rows["https://www.linkedin.com/in/newperson"]
        self.assertEqual((ana["first_name"], ana["last_name"], ana["position"], ana["company"], ana["connected_on"]),
                         ("Ana", "Silva", "Head of Growth", "Bloom", "2025-10-03"))
        lee = rows["https://www.linkedin.com/in/nocompany"]
        self.assertEqual((lee["position"], lee["company"]), ("Founder · Building something new", ""))
        # CSV data wins over the scraped headline
        jane = rows["https://www.linkedin.com/in/janedoe-example"]
        self.assertEqual((jane["company"], jane["connected_on"]), ("Acme Ventures", "2024-01-15"))

    def test_max(self):
        db = self.run_import("/mynetwork/", "--max", "3")
        self.assertEqual(self.count(db), 3)

    def test_pager_import_reads_everyone_without_scrolling(self):
        db = self.run_import("/pager/")
        rows = {r["url"]: r for r in db.execute("SELECT * FROM contacts")}
        self.assertEqual(len(rows), len(PEOPLE))
        ana = rows["https://www.linkedin.com/in/newperson"]
        self.assertEqual((ana["first_name"], ana["last_name"], ana["position"], ana["company"], ana["connected_on"]),
                         ("Ana", "Silva", "Head of Growth", "Bloom", "2025-10-03"))
        last = rows["https://www.linkedin.com/in/p11"]
        self.assertEqual((last["position"], last["company"], last["connected_on"]), ("Engineer", "Co11", "2022-05-02"))

    def test_pager_import_respects_max(self):
        db = self.run_import("/pager/", "--max", "12")
        self.assertEqual(self.count(db), 12)

    def test_stops_on_security_check(self):
        with self.assertRaises(lb.StopRun):
            self.run_import("/restricted")

    def test_waits_for_login_then_imports(self):
        # The connections URL shows a login wall first; once "logged in", it's retried and works.
        db = self.run_import("/login")
        self.assertEqual(self.count(db), len(PEOPLE))

    def test_stop_keeps_what_was_found(self):
        calls = []
        db = self.run_import(should_stop=lambda: bool(calls) or calls.append(1))  # true from the 2nd call on
        self.assertTrue(0 < self.count(db) < len(PEOPLE))

    def test_cli_saves_as_it_goes_and_stops_on_sigterm(self):
        # The web app runs the import as a subprocess and stops it with SIGTERM.
        import signal
        import subprocess
        import time
        proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "outreach.py"), "--db", self.dbpath, "import-linkedin", "--headless",
             "--url", self.base + "/mynetwork/", "--profile-dir", self.profile],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            if "connections so far" in line:
                break
        proc.send_signal(signal.SIGTERM)
        out = proc.stdout.read()
        proc.wait(30)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("Stopped.", out)
        self.assertIn("Imported", out)
        self.assertTrue(0 < self.count(outreach.connect(self.dbpath)) <= len(PEOPLE))

    def test_login_is_detected(self):
        lb.interactive_login(self.profile, headless=True, login_url=self.base + "/login")
        self.assertTrue(Handler.logged_in)


if __name__ == "__main__":
    unittest.main()
