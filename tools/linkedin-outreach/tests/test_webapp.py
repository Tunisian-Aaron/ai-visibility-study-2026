import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import webapp  # noqa: E402


class WebAppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = webapp.App(str(Path(cls.tmp.name) / "w.db"), str(Path(cls.tmp.name) / "profile"), None)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), None)
        port = cls.server.server_port
        cls.server.RequestHandlerClass = webapp.make_handler(cls.app, port)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{port}"
        cls.post("import", {"csv_text": (HERE / "sample_connections.csv").read_text()})

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    @classmethod
    def post(cls, route, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-Outreach": "1"}
        h.update(headers or {})
        req = urllib.request.Request(f"{cls.base}/api/{route}", data=json.dumps(body or {}).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def draft_body(self, **vars_):
        return {"campaign": "web", "template": "Hi {first_name}, try {sender_product}. - {sender_name}",
                "sender_name": "Sam", "vars": vars_, "filters": {"title": "partner, principal"}}

    def test_rejects_cross_site_requests(self):
        self.assertEqual(self.post("state", headers={"X-Outreach": ""})[0], 403)
        self.assertEqual(self.post("state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.post("state")[0], 200)

    def test_page_served(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"LinkedIn Outreach", r.read())

    def test_blank_fields_block_drafting(self):
        status, r = self.post("preview", self.draft_body(product=""))
        self.assertEqual(status, 400)
        self.assertIn("{sender_product}", r["error"])

    def test_preview_draft_and_edit(self):
        status, r = self.post("preview", self.draft_body(product="Navoy"))
        self.assertEqual((status, r["count"]), (200, 2))
        self.assertEqual(r["previews"][0]["body"], "Hi Ana, try Navoy. - Sam")
        self.assertEqual(self.post("draft", self.draft_body(product="Navoy"))[0], 200)
        _, m = self.post("messages", {"campaign": "web", "status": "draft"})
        self.assertEqual(len(m["messages"]), 2)
        mid = m["messages"][0]["id"]
        self.post("message", {"id": mid, "body": "Edited"})
        self.post("message", {"id": m["messages"][1]["id"], "status": "skipped"})
        _, m = self.post("messages", {"campaign": "web", "status": "draft"})
        self.assertEqual([x["body"] for x in m["messages"]], ["Edited"])
        _, st = self.post("state")
        self.assertEqual(st["campaigns"][0]["counts"]["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
