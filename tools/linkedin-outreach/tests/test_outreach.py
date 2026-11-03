import csv
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import outreach  # noqa: E402

SAMPLE = HERE / "sample_connections.csv"
TEMPLATE = HERE / "templates" / "product-beta.txt"


class OutreachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbpath = str(Path(self.tmp.name) / "t.db")
        self.run_cli("import", str(SAMPLE))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        outreach.main(["--db", self.dbpath, *argv])

    def db(self):
        return outreach.connect(self.dbpath)

    def draft_product(self, *extra):
        self.run_cli("draft", "beta", "--template", str(TEMPLATE), "--sender-name", "Sam",
                     "--var", "product=Navoy", "--var", "audience=founders",
                     "--var", "problem=AI search visibility", *extra)

    def test_import_skips_preamble_and_is_idempotent(self):
        self.run_cli("import", str(SAMPLE))
        rows = self.db().execute("SELECT * FROM contacts ORDER BY id").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["first_name"], "Jane")
        self.assertEqual(rows[0]["connected_on"], "2024-01-15")

    def test_filters(self):
        args = outreach.build_parser().parse_args(
            ["list", "--title", "partner", "--title", "principal", "--exclude-company", "northwind"])
        names = [r["first_name"] for r in outreach.select_contacts(self.db(), args)]
        self.assertEqual(names, ["Jane"])

    def test_render_fallbacks(self):
        c = self.db().execute("SELECT * FROM contacts WHERE first_name='Tom'").fetchone()
        out = outreach.render("Hi {first_name} at {company|your team}, from {sender_name}", c, {"name": "Sam"})
        self.assertEqual(out, "Hi Tom at your team, from Sam")
        with self.assertRaises(KeyError):
            outreach.render("{sender_product}", c, {"name": "Sam"})

    def test_missing_var_is_a_clear_error(self):
        with self.assertRaises(SystemExit):
            self.run_cli("draft", "beta", "--template", str(TEMPLATE))

    def test_draft_dedupes_and_respects_dnc(self):
        self.run_cli("dnc", "3")
        self.draft_product("--title", "partner", "--title", "engineer")
        self.draft_product()  # second run only adds new people
        db = self.db()
        bodies = db.execute("SELECT contact_id, body FROM messages").fetchall()
        self.assertEqual(sorted(r["contact_id"] for r in bodies), [1, 2, 4, 5])
        self.assertIn("Hi Jane", bodies[0]["body"])
        self.assertIn("Navoy", bodies[0]["body"])

    def test_send_loop_cap_and_actions(self):
        self.draft_product()
        answers = iter(["", "s", "x", "", "q"])
        args = outreach.build_parser().parse_args(["--db", self.dbpath, "send", "beta", "--daily-cap", "3"])
        db = self.db()
        opened = []
        outreach.cmd_send(db, args, prompt=lambda _: next(answers),
                          clipboard=lambda _: True, opener=opened.append)
        statuses = [r["status"] for r in db.execute("SELECT status FROM messages ORDER BY id")]
        self.assertEqual(statuses, ["sent", "skipped", "skipped", "sent", "draft"])
        self.assertEqual(outreach.sent_last_24h(db), 2)
        dnc = db.execute("SELECT first_name FROM contacts WHERE do_not_contact=1").fetchall()
        self.assertEqual(len(dnc), 1)
        self.assertEqual(len(opened), 5)

        # cap of 2 already reached: the loop must stop without prompting
        capped = outreach.build_parser().parse_args(["--db", self.dbpath, "send", "beta", "--daily-cap", "2"])
        def no_prompt(_):
            raise AssertionError("should not prompt once the cap is reached")
        outreach.cmd_send(db, capped, prompt=no_prompt, clipboard=lambda _: True, opener=lambda _: None)
        self.assertEqual(outreach.sent_last_24h(db), 2)

    def test_cooldown_skips_recently_messaged(self):
        self.draft_product()
        self.run_cli("mark", "beta", "1", "sent")
        self.run_cli("draft", "followup", "--template", str(TEMPLATE), "--sender-name", "Sam",
                     "--var", "product=X", "--var", "audience=Y", "--var", "problem=Z")
        ids = [r[0] for r in self.db().execute(
            "SELECT contact_id FROM messages m JOIN campaigns c ON c.id=m.campaign_id WHERE c.name='followup'")]
        self.assertNotIn(1, ids)
        self.assertEqual(len(ids), 4)

    def test_export_import_roundtrip(self):
        self.draft_product()
        out = str(Path(self.tmp.name) / "drafts.csv")
        self.run_cli("export", "beta", out)
        rows = list(csv.DictReader(open(out, encoding="utf-8")))
        rows[0]["message"] = "Custom note for Jane"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        self.run_cli("import-drafts", "beta", out)
        body = self.db().execute("SELECT body FROM messages WHERE contact_id=?",
                                 (int(rows[0]["contact_id"]),)).fetchone()[0]
        self.assertEqual(body, "Custom note for Jane")


if __name__ == "__main__":
    unittest.main()
