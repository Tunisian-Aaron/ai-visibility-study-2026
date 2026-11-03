#!/usr/bin/env python3
"""LinkedIn outreach assistant.

Imports your LinkedIn connections export, segments it, drafts personalized
messages for a campaign (template-based, optionally refined by Claude), and
walks you through sending them one at a time: the message is copied to your
clipboard and the person's profile opens in your browser, you paste and press
Send in LinkedIn yourself, and the tool records the result.

Two ways to send:
  send      assisted - you press Send in LinkedIn yourself (no automation)
  autosend  fully automated - drives your logged-in browser and clicks Send,
            with a daily cap, pacing between messages, and a hard stop on any
            LinkedIn security check. Automated messaging is against LinkedIn's
            User Agreement and can get your account restricted.
Run `python outreach.py --help` for usage.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import webbrowser
from pathlib import Path

DEFAULT_DB = os.environ.get("OUTREACH_DB", "outreach.db")
DEFAULT_DAILY_CAP = 25
LINKEDIN_MAX_CHARS = 8000
AI_MODEL = "claude-opus-5-5"

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    first_name TEXT, last_name TEXT, url TEXT, email TEXT,
    company TEXT, position TEXT, connected_on TEXT,
    do_not_contact INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    template TEXT NOT NULL,
    brief TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | sent | skipped | replied | failed
    sent_at TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (campaign_id, contact_id)
);
"""

STATUSES = ("draft", "sent", "skipped", "replied", "failed")


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    if "error" not in cols:  # databases created before autosend existed
        db.execute("ALTER TABLE messages ADD COLUMN error TEXT")
    return db


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def parse_date(value: str) -> str | None:
    value = (value or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def read_connections_csv(path: str) -> list[dict]:
    """Read LinkedIn's Connections.csv, skipping its 'Notes:' preamble."""
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if l.lstrip().startswith("First Name")), None)
    if start is None:
        raise SystemExit(f"{path}: no 'First Name' header row found - is this LinkedIn's Connections.csv?")
    rows = []
    for r in csv.DictReader(io.StringIO("".join(lines[start:]))):
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        if not (r.get("First Name") or r.get("Last Name")):
            continue
        rows.append({
            "first_name": r.get("First Name", ""),
            "last_name": r.get("Last Name", ""),
            "url": r.get("URL", ""),
            "email": r.get("Email Address", ""),
            "company": r.get("Company", ""),
            "position": r.get("Position", ""),
            "connected_on": parse_date(r.get("Connected On", "")),
        })
    return rows


def cmd_import(db, args):
    rows = read_connections_csv(args.csv)
    added = updated = 0
    for r in rows:
        key = r["url"].rstrip("/").lower() or f"name:{r['first_name']} {r['last_name']}".lower()
        exists = db.execute("SELECT 1 FROM contacts WHERE key=?", (key,)).fetchone()
        db.execute(
            """INSERT INTO contacts (key, first_name, last_name, url, email, company, position, connected_on)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET first_name=excluded.first_name, last_name=excluded.last_name,
                 url=excluded.url, email=excluded.email, company=excluded.company,
                 position=excluded.position, connected_on=excluded.connected_on""",
            (key, r["first_name"], r["last_name"], r["url"], r["email"],
             r["company"], r["position"], r["connected_on"]),
        )
        updated += bool(exists)
        added += not exists
    db.commit()
    print(f"Imported {len(rows)} connections ({added} new, {updated} updated).")


# --------------------------------------------------------------------------
# Segmenting
# --------------------------------------------------------------------------

def add_filter_args(p):
    g = p.add_argument_group("filters (repeatable flags are OR-ed; matching is case-insensitive substring)")
    g.add_argument("--company", action="append", default=[], help="company contains")
    g.add_argument("--title", action="append", default=[], help="position/title contains")
    g.add_argument("--exclude-company", action="append", default=[])
    g.add_argument("--exclude-title", action="append", default=[])
    g.add_argument("--since", help="connected on or after YYYY-MM-DD")
    g.add_argument("--before", help="connected before YYYY-MM-DD")
    g.add_argument("--ids", help="comma-separated contact ids")
    g.add_argument("--limit", type=int)


def select_contacts(db, args, extra_where: str = "", extra_params=()):
    where, params = ["do_not_contact = 0"], []

    def any_like(col, values, negate=False):
        if not values:
            return
        clause = " OR ".join(f"LOWER(COALESCE({col},'')) LIKE ?" for _ in values)
        where.append(f"NOT ({clause})" if negate else f"({clause})")
        params.extend(f"%{v.lower()}%" for v in values)

    any_like("company", args.company)
    any_like("position", args.title)
    any_like("company", args.exclude_company, negate=True)
    any_like("position", args.exclude_title, negate=True)
    if args.since:
        where.append("connected_on >= ?")
        params.append(args.since)
    if args.before:
        where.append("connected_on < ?")
        params.append(args.before)
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if extra_where:
        where.append(extra_where)
        params.extend(extra_params)
    sql = f"SELECT * FROM contacts WHERE {' AND '.join(where)} ORDER BY connected_on DESC, id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    return db.execute(sql, params).fetchall()


def cmd_list(db, args):
    rows = select_contacts(db, args)
    for c in rows:
        name = f"{c['first_name']} {c['last_name']}"
        print(f"{c['id']:>5}  {name[:26]:<26}  {c['position'][:40]:<40}  "
              f"{c['company'][:28]:<28}  {c['connected_on'] or ''}")
    print(f"\n{len(rows)} contact(s) match.")


def cmd_dnc(db, args):
    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    db.executemany("UPDATE contacts SET do_not_contact=? WHERE id=?", [(0 if args.undo else 1, i) for i in ids])
    db.commit()
    print(f"{'Cleared' if args.undo else 'Set'} do-not-contact on {len(ids)} contact(s).")


# --------------------------------------------------------------------------
# Drafting
# --------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{(\w+)(?:\|([^}]*))?\}")


def render(template: str, contact, sender: dict) -> str:
    """Fill {field} / {field|fallback} placeholders from the contact and sender."""
    values = {k: (contact[k] or "") for k in contact.keys()}
    values.update({f"sender_{k}": v for k, v in sender.items()})
    values["first_name"] = values.get("first_name", "").split(" ")[0].title() if values.get("first_name") else ""

    def sub(m):
        val = values.get(m.group(1))
        if val is None and m.group(2) is None:
            raise KeyError(f"Unknown placeholder {{{m.group(1)}}}")
        return val or (m.group(2) or "")

    return PLACEHOLDER.sub(sub, template).strip()


def personalize_with_claude(client, base: str, contact, brief: str) -> str:
    """Ask Claude to lightly tailor the rendered message. Returns base on any refusal."""
    profile = (f"Name: {contact['first_name']} {contact['last_name']}\n"
               f"Position: {contact['position'] or 'unknown'}\n"
               f"Company: {contact['company'] or 'unknown'}")
    prompt = (
        "You help someone send a short LinkedIn message to one of their existing connections.\n\n"
        f"Campaign goal:\n{brief}\n\n"
        f"Recipient (from LinkedIn - this is everything known about them):\n{profile}\n\n"
        f"Draft message:\n{base}\n\n"
        "Rewrite the draft so it reads as written for this person: tie the ask to their role or "
        "company where that is natural, keep the sender's voice, the core ask and any links, and "
        "keep it under 120 words. Use only the facts above - never invent shared history, mutual "
        "contacts or details about the recipient. Reply with the message text only."
    )
    resp = client.beta.messages.create(
        model=AI_MODEL,
        max_tokens=2048,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        return base
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text or base


def draft_candidates(db, args):
    """Contacts matching the filters who aren't in this campaign and weren't messaged recently."""
    cutoff = (dt.datetime.now() - dt.timedelta(days=args.cooldown_days)).isoformat(timespec="seconds")
    extra = ("id NOT IN (SELECT contact_id FROM messages m JOIN campaigns c ON c.id=m.campaign_id "
             "WHERE c.name=?) AND id NOT IN (SELECT contact_id FROM messages WHERE sent_at >= ?)")
    return select_contacts(db, args, extra, (args.campaign, cutoff))


def sender_values(args) -> dict:
    sender = {"name": args.sender_name or os.environ.get("OUTREACH_SENDER_NAME", "")}
    for kv in args.var:
        k, sep, v = kv.partition("=")
        if not sep:
            raise SystemExit(f"--var expects key=value, got {kv!r}")
        sender[k.strip()] = v.strip()
    return sender


def cmd_draft(db, args):
    template = getattr(args, "template_text", None) or (
        Path(args.template).read_text(encoding="utf-8") if args.template else None)
    camp = db.execute("SELECT * FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    if camp is None:
        if template is None:
            raise SystemExit("New campaign: pass --template FILE.")
        if not args.dry_run:
            db.execute("INSERT INTO campaigns (name, template, brief, created_at) VALUES (?,?,?,?)",
                       (args.campaign, template, args.brief, now()))
            camp = db.execute("SELECT * FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    template = template or camp["template"]
    brief = args.brief or (camp["brief"] if camp else None) or ""

    contacts = draft_candidates(db, args)
    if not contacts:
        print("No new contacts match these filters.")
        return

    client = None
    if args.ai:
        try:
            import anthropic
        except ImportError:
            raise SystemExit("--ai needs the Anthropic SDK: pip install anthropic")
        client = anthropic.Anthropic()
        if not brief:
            raise SystemExit("--ai needs --brief describing the goal of the campaign.")

    sender = sender_values(args)
    shown = 0
    for i, c in enumerate(contacts, 1):
        try:
            body = render(template, c, sender)
        except KeyError as e:
            raise SystemExit(f"{e.args[0]} - pass it with --var name=value (sender_* placeholders) "
                             "or give it a fallback like {name|default}.")
        if client is not None:
            try:
                body = personalize_with_claude(client, body, c, brief)
            except Exception as e:  # keep the template draft rather than abort the batch
                print(f"  ! AI personalization failed for #{c['id']} ({type(e).__name__}: {e}); using template.")
        if len(body) > LINKEDIN_MAX_CHARS:
            print(f"  ! #{c['id']}: message is {len(body)} chars (LinkedIn limit {LINKEDIN_MAX_CHARS}).")
        if args.dry_run:
            if shown < 3:
                print(f"--- #{c['id']} {c['first_name']} {c['last_name']} ---\n{body}\n")
                shown += 1
            continue
        db.execute("INSERT OR IGNORE INTO messages (campaign_id, contact_id, body, updated_at) VALUES (?,?,?,?)",
                   (camp["id"], c["id"], body, now()))
        if client is not None:
            print(f"  drafted {i}/{len(contacts)}", end="\r")
    if args.dry_run:
        print(f"(dry run) {len(contacts)} message(s) would be drafted; nothing saved.")
        return
    db.commit()
    print(f"Drafted {len(contacts)} message(s) in campaign '{args.campaign}'. "
          f"Review with: outreach.py review {args.campaign}")


# --------------------------------------------------------------------------
# Review / export / status
# --------------------------------------------------------------------------

def campaign_messages(db, name, status=None):
    sql = ("SELECT m.*, c.first_name, c.last_name, c.url, c.company, c.position FROM messages m "
           "JOIN campaigns k ON k.id=m.campaign_id JOIN contacts c ON c.id=m.contact_id "
           "WHERE k.name=? AND c.do_not_contact=0")
    params = [name]
    if status:
        sql += " AND m.status=?"
        params.append(status)
    return db.execute(sql + " ORDER BY m.id", params).fetchall()


def cmd_review(db, args):
    rows = campaign_messages(db, args.campaign, args.status)
    for m in rows:
        print(f"=== contact #{m['contact_id']}  {m['first_name']} {m['last_name']}  "
              f"[{m['status']}]  {m['position']} @ {m['company']}\n{m['body']}\n")
    print(f"{len(rows)} message(s).")


def cmd_export(db, args):
    rows = campaign_messages(db, args.campaign, args.status)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contact_id", "first_name", "last_name", "url", "company", "position", "status", "message"])
        for m in rows:
            w.writerow([m["contact_id"], m["first_name"], m["last_name"], m["url"], m["company"],
                        m["position"], m["status"], m["body"]])
    print(f"Wrote {len(rows)} row(s) to {args.out}.")


def cmd_import_drafts(db, args):
    """Replace draft bodies from a CSV with contact_id + message columns (e.g. an edited export)."""
    camp = db.execute("SELECT id FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    if camp is None:
        raise SystemExit(f"No campaign '{args.campaign}'.")
    updated = skipped = 0
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            body = (r.get("message") or "").strip()
            if not body or not (r.get("contact_id") or "").strip().isdigit():
                skipped += 1
                continue
            cur = db.execute("UPDATE messages SET body=?, updated_at=? WHERE campaign_id=? AND contact_id=? "
                             "AND status='draft'", (body, now(), camp["id"], int(r["contact_id"])))
            updated += cur.rowcount
            skipped += not cur.rowcount
    db.commit()
    print(f"Updated {updated} draft(s); skipped {skipped} row(s) (empty, unknown, or already sent).")


def cmd_status(db, args):
    camps = db.execute("SELECT * FROM campaigns ORDER BY id").fetchall()
    if not camps:
        print("No campaigns yet.")
    for k in camps:
        counts = dict(db.execute("SELECT status, COUNT(*) FROM messages WHERE campaign_id=? GROUP BY status",
                                 (k["id"],)).fetchall())
        sent = counts.get("sent", 0) + counts.get("replied", 0)
        rate = f"{100 * counts.get('replied', 0) / sent:.0f}%" if sent else "-"
        print(f"{k['name']:<24} " + "  ".join(f"{s}={counts.get(s, 0)}" for s in STATUSES)
              + f"  reply rate={rate}")
    print(f"\nSent in the last 24h (all campaigns): {sent_last_24h(db)}")


def cmd_mark(db, args):
    camp = db.execute("SELECT id FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    if camp is None:
        raise SystemExit(f"No campaign '{args.campaign}'.")
    ids = [int(x) for x in args.contact_ids.split(",")]
    for cid in ids:
        db.execute("UPDATE messages SET status=?, updated_at=?, sent_at=COALESCE(sent_at, CASE WHEN ? IN "
                   "('sent','replied') THEN ? END) WHERE campaign_id=? AND contact_id=?",
                   (args.status, now(), args.status, now(), camp["id"], cid))
    db.commit()
    print(f"Marked {len(ids)} message(s) as {args.status}.")


# --------------------------------------------------------------------------
# Assisted sending
# --------------------------------------------------------------------------

def sent_last_24h(db) -> int:
    cutoff = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    return db.execute("SELECT COUNT(*) FROM messages WHERE sent_at >= ?", (cutoff,)).fetchone()[0]


def copy_to_clipboard(text: str) -> bool:
    candidates = [["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"],
                  ["xsel", "--clipboard", "--input"], ["clip.exe"], ["clip"]]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
                return True
            except (subprocess.SubprocessError, OSError):
                continue
    return False


def edit_text(text: str, prompt=input) -> str:
    editor = os.environ.get("EDITOR")
    if editor:
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            path = f.name
        subprocess.call([*editor.split(), path])
        new = Path(path).read_text(encoding="utf-8").strip()
        os.unlink(path)
        return new or text
    print("Type the new message; finish with a line containing only '.'")
    lines = []
    while (line := prompt("")) != ".":
        lines.append(line)
    return "\n".join(lines).strip() or text


def cmd_send(db, args, prompt=input, clipboard=copy_to_clipboard, opener=webbrowser.open):
    queue = campaign_messages(db, args.campaign, "draft")
    if not queue:
        print("Nothing left to send in this campaign.")
        return
    print(f"{len(queue)} draft(s) queued. Daily cap: {args.daily_cap} "
          f"(already sent in the last 24h: {sent_last_24h(db)}).\n"
          "For each one: the message is copied to your clipboard and the profile opens.\n"
          "In LinkedIn click Message, paste, send - then come back here.\n")
    for m in queue:
        if sent_last_24h(db) >= args.daily_cap:
            print(f"Daily cap of {args.daily_cap} reached - stopping. Run again tomorrow.")
            break
        body = m["body"]
        while True:
            print(f"--- {m['first_name']} {m['last_name']}  ({m['position']} @ {m['company']})\n"
                  f"{m['url']}\n\n{body}\n")
            copied = clipboard(body)
            if not copied:
                print("(clipboard unavailable - copy the text above manually)")
            if m["url"] and not args.no_open:
                opener(m["url"])
            choice = prompt("[Enter] sent  [s]kip  [e]dit  [x] never contact  [q]uit > ").strip().lower()
            if choice == "e":
                body = edit_text(body, prompt)
                db.execute("UPDATE messages SET body=?, updated_at=? WHERE id=?", (body, now(), m["id"]))
                db.commit()
                continue
            break
        if choice == "q":
            break
        if choice == "x":
            db.execute("UPDATE contacts SET do_not_contact=1 WHERE id=?", (m["contact_id"],))
            db.execute("UPDATE messages SET status='skipped', updated_at=? WHERE id=?", (now(), m["id"]))
        elif choice == "s":
            db.execute("UPDATE messages SET status='skipped', updated_at=? WHERE id=?", (now(), m["id"]))
        else:
            db.execute("UPDATE messages SET status='sent', sent_at=?, updated_at=? WHERE id=?",
                       (now(), now(), m["id"]))
        db.commit()
    remaining = len(campaign_messages(db, args.campaign, "draft"))
    print(f"\nDone for now. {remaining} draft(s) remaining; sent in last 24h: {sent_last_24h(db)}.")


# --------------------------------------------------------------------------
# Fully automated sending
# --------------------------------------------------------------------------

def cmd_login(db, args):
    import linkedin_browser as lb
    lb.interactive_login(args.profile_dir, channel=args.channel)


def cmd_autosend(db, args, sender_factory=None, sleep=time.sleep, prompt=input):
    import linkedin_browser as lb
    if args.min_delay > args.max_delay:
        raise SystemExit("--min-delay must be <= --max-delay")
    queue = campaign_messages(db, args.campaign, "draft")
    room = args.daily_cap - sent_last_24h(db)
    if not queue:
        print("Nothing left to send in this campaign.")
        return
    if room <= 0 and not args.dry_run:
        print(f"Daily cap of {args.daily_cap} already reached - try again later.")
        return
    batch = queue[:max(room, 0) if not args.dry_run else len(queue)]
    if args.max:
        batch = batch[:args.max]
    avg = (args.min_delay + args.max_delay) / 2
    print(f"{'DRY RUN - nothing will be sent. ' if args.dry_run else ''}"
          f"{len(batch)} of {len(queue)} draft(s) in '{args.campaign}' this run "
          f"(cap {args.daily_cap}/24h, {sent_last_24h(db)} already sent). "
          f"Pacing {args.min_delay:.0f}-{args.max_delay:.0f}s between messages, "
          f"about {len(batch) * avg / 60:.0f} min total.")
    if not args.dry_run and not args.yes:
        if prompt("Type 'send' to start sending from your LinkedIn account: ").strip().lower() != "send":
            print("Aborted.")
            return

    if sender_factory is None:
        def sender_factory():
            return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)

    sent = failed = consecutive = 0
    shots = Path(args.screenshots)
    with sender_factory() as page:
        sender = lb.LinkedInSender(page)
        for i, m in enumerate(batch):
            if i:
                delay = random.uniform(args.min_delay, args.max_delay)
                print(f"  waiting {delay:.0f}s...")
                sleep(delay)
            who = f"{m['first_name']} {m['last_name']}"
            try:
                sender.send(m["url"], m["body"], dry_run=args.dry_run)
            except lb.StopRun as e:
                print(f"STOPPED: {e}")
                sender.screenshot(str(shots / f"stopped-{m['contact_id']}.png"))
                break
            except Exception as e:  # one bad profile shouldn't kill the run
                failed += 1
                consecutive += 1
                msg = f"{type(e).__name__}: {e}"
                print(f"  x {who}: {msg}")
                sender.screenshot(str(shots / f"failed-{m['contact_id']}.png"))
                if not args.dry_run:
                    db.execute("UPDATE messages SET status='failed', error=?, updated_at=? WHERE id=?",
                               (msg, now(), m["id"]))
                    db.commit()
                if consecutive >= 3:
                    print("STOPPED: 3 failures in a row - LinkedIn's page layout may have changed. "
                          f"See screenshots in {shots}/.")
                    break
                continue
            consecutive = 0
            sent += 1
            if args.dry_run:
                print(f"  ok {who} (dry run - typed, not sent)")
            else:
                db.execute("UPDATE messages SET status='sent', sent_at=?, error=NULL, updated_at=? WHERE id=?",
                           (now(), now(), m["id"]))
                db.commit()
                print(f"  sent {who}")
    verb = "checked" if args.dry_run else "sent"
    print(f"\nDone: {sent} {verb}, {failed} failed. "
          f"{len(campaign_messages(db, args.campaign, 'draft'))} draft(s) remaining.")
    if failed and not args.dry_run:
        print(f"Failed messages are marked 'failed' (see `review {args.campaign} --status failed`); "
              f"requeue with `mark {args.campaign} <ids> draft`.")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="import LinkedIn's Connections.csv")
    s.add_argument("csv")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("list", help="list contacts matching filters")
    add_filter_args(s)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("draft", help="create/extend a campaign and draft messages")
    s.add_argument("campaign")
    s.add_argument("--template", help="template file (required for a new campaign)")
    s.add_argument("--brief", help="one-paragraph goal of the campaign (used by --ai)")
    s.add_argument("--sender-name", help="fills {sender_name}")
    s.add_argument("--var", action="append", default=[], metavar="KEY=VALUE",
                   help="fills {sender_KEY}, e.g. --var product=Navoy (repeatable)")
    s.add_argument("--ai", action="store_true", help="personalize each draft with Claude (needs ANTHROPIC_API_KEY)")
    s.add_argument("--cooldown-days", type=int, default=30,
                   help="skip anyone messaged in any campaign within N days (default 30)")
    s.add_argument("--dry-run", action="store_true", help="preview 3 drafts, save nothing")
    add_filter_args(s)
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("review", help="print a campaign's messages")
    s.add_argument("campaign")
    s.add_argument("--status", choices=STATUSES)
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("export", help="export a campaign to CSV")
    s.add_argument("campaign")
    s.add_argument("out")
    s.add_argument("--status", choices=STATUSES)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import-drafts", help="load edited/personalized drafts back from CSV")
    s.add_argument("campaign")
    s.add_argument("csv", help="CSV with contact_id and message columns (same shape as export)")
    s.set_defaults(func=cmd_import_drafts)

    s = sub.add_parser("send", help="assisted send loop (you press Send in LinkedIn)")
    s.add_argument("campaign")
    s.add_argument("--daily-cap", type=int, default=DEFAULT_DAILY_CAP,
                   help=f"max messages per rolling 24h across campaigns (default {DEFAULT_DAILY_CAP})")
    s.add_argument("--no-open", action="store_true", help="don't open profiles in the browser")
    s.set_defaults(func=cmd_send)

    def browser_args(sp):
        sp.add_argument("--profile-dir", default=None,
                        help="browser profile holding your LinkedIn login (default ~/.outreach-linkedin-profile)")
        sp.add_argument("--channel", choices=["chrome", "msedge"],
                        help="use your installed Chrome/Edge instead of Playwright's Chromium")

    s = sub.add_parser("login", help="log in to LinkedIn once in the automation browser")
    browser_args(s)
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("autosend", help="FULLY AUTOMATED: send drafts from your browser session")
    s.add_argument("campaign")
    browser_args(s)
    s.add_argument("--daily-cap", type=int, default=20,
                   help="max messages per rolling 24h across campaigns (default 20)")
    s.add_argument("--max", type=int, help="max messages this run")
    s.add_argument("--min-delay", type=float, default=60, help="min seconds between messages (default 60)")
    s.add_argument("--max-delay", type=float, default=180, help="max seconds between messages (default 180)")
    s.add_argument("--dry-run", action="store_true",
                   help="open each profile and type the message, but don't press Send")
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt (for scheduled runs)")
    s.add_argument("--screenshots", default="failures", help="where to save failure screenshots")
    s.set_defaults(func=cmd_autosend)

    s = sub.add_parser("mark", help="set message status, e.g. when someone replies")
    s.add_argument("campaign")
    s.add_argument("contact_ids", help="comma-separated contact ids")
    s.add_argument("status", choices=STATUSES)
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("dnc", help="mark contacts do-not-contact (or --undo)")
    s.add_argument("ids", help="comma-separated contact ids")
    s.add_argument("--undo", action="store_true")
    s.set_defaults(func=cmd_dnc)

    s = sub.add_parser("status", help="per-campaign counts and reply rate")
    s.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "profile_dir", "unset") is None:
        import linkedin_browser as lb
        args.profile_dir = lb.DEFAULT_PROFILE_DIR
    db = connect(args.db)
    try:
        args.func(db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
