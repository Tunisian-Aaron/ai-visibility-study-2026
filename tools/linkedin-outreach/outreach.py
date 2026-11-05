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
import contextlib
import csv
import datetime as dt
import io
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

def load_env_file(path: Path = Path(__file__).resolve().parent / ".env"):
    """Read KEY=VALUE lines from a .env next to this file (gitignored), without overriding
    the real environment. Keeps the API key out of shell history and the web app's command."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.partition("=")[::2]
        k, v = k.strip(), v.strip().strip("'\"")
        if v and (not os.environ.get(k) or os.environ[k].endswith("...")):  # "..." = unfilled placeholder
            os.environ[k] = v


load_env_file()

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
CREATE TABLE IF NOT EXISTS profiles (  -- what `enrich` read from each person's LinkedIn profile
    contact_id INTEGER PRIMARY KEY REFERENCES contacts(id),
    headline TEXT, location TEXT, about TEXT, experience TEXT, education TEXT,
    fetched_at TEXT NOT NULL
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
    if "ai_at" not in cols:  # ...and before `personalize`
        db.execute("ALTER TABLE messages ADD COLUMN ai_at TEXT")
    if "base_body" not in cols:  # the template text, before Claude rewrote it
        db.execute("ALTER TABLE messages ADD COLUMN base_body TEXT")
        db.execute("UPDATE messages SET base_body=body")
    return db


def stop_flag() -> threading.Event:
    """Set when webapp.py stops this process with SIGTERM. Only a flag: interrupting a
    browser call mid-flight can leave Playwright unable to close the browser cleanly."""
    stop = threading.Event()
    with contextlib.suppress(ValueError):  # only possible from the main thread
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return stop


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def parse_date(value: str) -> str | None:
    value = (value or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y"):
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


CONTACT_FIELDS = ("first_name", "last_name", "url", "email", "company", "position", "connected_on")


def save_contacts(db, rows, fill_blanks_only=False) -> tuple[int, int]:
    """Insert or update contacts by profile URL. fill_blanks_only keeps existing values
    (used for the scraped list, which is less complete than LinkedIn's CSV export)."""
    if fill_blanks_only:
        sets = ", ".join(f"{c}=COALESCE(NULLIF({c}, ''), excluded.{c})" for c in CONTACT_FIELDS)
    else:
        sets = ", ".join(f"{c}=excluded.{c}" for c in CONTACT_FIELDS)
    added = updated = 0
    for r in rows:
        key = r["url"].rstrip("/").lower() or f"name:{r['first_name']} {r['last_name']}".lower()
        exists = db.execute("SELECT 1 FROM contacts WHERE key=?", (key,)).fetchone()
        db.execute(
            f"""INSERT INTO contacts (key, {", ".join(CONTACT_FIELDS)}) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET {sets}""",
            (key, *(r[c] for c in CONTACT_FIELDS)),
        )
        updated += bool(exists)
        added += not exists
    db.commit()
    return added, updated


def cmd_import(db, args):
    rows = read_connections_csv(args.csv)
    added, updated = save_contacts(db, rows)
    print(f"Imported {len(rows)} connections ({added} new, {updated} updated).")


def cmd_import_linkedin(db, args, page_factory=None, should_stop=lambda: False, sleep=time.sleep):
    import linkedin_browser as lb
    if page_factory is None:
        def page_factory():
            return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)
    # Each batch is saved as soon as it's read, so a stop, a crash or a kill loses nothing.
    stats = {"n": 0, "added": 0, "updated": 0}

    def save(batch):
        rows = [{**{k: c[k] for k in ("first_name", "last_name", "url", "company", "position")},
                 "email": "", "connected_on": parse_date(c["connected_on"])} for c in batch]
        added, updated = save_contacts(db, rows, fill_blanks_only=True)
        stats["n"] += len(rows)
        stats["added"] += added
        stats["updated"] += updated

    stop = stop_flag()
    print("Reading your connections list on LinkedIn (read-only, scrolls through it)...")
    try:
        with page_factory() as page:
            lb.collect_connections(page, url=args.url or lb.CONNECTIONS_URL, max_contacts=args.max,
                                   sleep=sleep, should_stop=lambda: stop.is_set() or should_stop(), on_batch=save)
    except KeyboardInterrupt:
        stop.set()
    finally:
        if stop.is_set():
            print("Stopped.")
        if stats["n"]:
            print(f"Imported {stats['n']} connections from LinkedIn ({stats['added']} new, "
                  f"{stats['updated']} already known). Title and company come from each person's "
                  "headline, so they're approximate.")


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


# --- AI personalization ------------------------------------------------------------
# Two ways to reach Claude: the API (ANTHROPIC_API_KEY + `pip install anthropic`, billed per
# token) or the Claude Code CLI already logged in on this computer (uses that plan's usage).

class ClaudeAPI:
    name = "the Claude API"

    def __init__(self):
        import anthropic  # ImportError if not installed
        self.client = anthropic.Anthropic()

    def generate(self, prompt: str) -> str | None:
        resp = self.client.beta.messages.create(
            model=AI_MODEL,
            max_tokens=2048,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip()


class ClaudeCLI:
    name = "Claude Code"

    def __init__(self):
        self.exe = shutil.which("claude")
        if not self.exe:
            raise FileNotFoundError("claude")

    def generate(self, prompt: str) -> str | None:
        r = subprocess.run([self.exe, "-p", "Follow the instructions in the input.", "--output-format", "text",
                            "--model", AI_MODEL, "--bare", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, timeout=240, cwd=tempfile.gettempdir())
        if r.returncode:
            detail = (r.stderr or r.stdout).strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"claude exited with {r.returncode}")
        return r.stdout.strip()


def ai_backend():
    """The Claude API when ANTHROPIC_API_KEY is set, else the Claude Code CLI if installed."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeAPI()
        except ImportError:
            raise SystemExit("ANTHROPIC_API_KEY is set but the SDK is missing: pip install anthropic")
    if shutil.which("claude"):
        return ClaudeCLI()
    raise SystemExit("Personalizing needs Claude. Either set ANTHROPIC_API_KEY (and pip install anthropic), "
                     "or install Claude Code (https://claude.com/claude-code) and run `claude login`.")


def ai_backend_name() -> str | None:
    try:
        return ai_backend().name
    except SystemExit:
        return None


def personalization_prompt(base: str, contact, brief: str, profile=None) -> str:
    facts = [f"Name: {contact['first_name']} {contact['last_name']}",
             f"Headline: {(profile and profile['headline']) or contact['position'] or 'unknown'}",
             f"Company: {contact['company'] or 'unknown'}"]
    if profile:
        for label, key in (("Location", "location"), ("About (their own words)", "about"),
                           ("Experience", "experience"), ("Education", "education")):
            if profile[key]:
                facts.append(f"{label}:\n{profile[key]}")
    return (
        "You help someone send a short LinkedIn message to one of their existing connections.\n\n"
        f"Campaign goal:\n{brief}\n\n"
        "Recipient, from their LinkedIn profile. This is everything known about them:\n"
        + "\n".join(facts) + "\n\n"
        f"Draft message:\n{base}\n\n"
        "Rewrite the draft so it reads as written for this person:\n"
        "- Keep the draft's structure, voice, core ask, links and sign-off.\n"
        f"- Keep the length: the draft is {len(base.split())} words, and the result must not be longer. "
        "If you add a personal sentence, cut or shorten another one.\n"
        "- Change only what makes it personal: open on something specific from their experience, "
        "about text or education, and tie the ask to it where that is natural. One or two concrete "
        "details beat a list.\n"
        "- Use only the facts above. Never invent shared history, mutual contacts or anything not "
        "stated. If the profile says little, change little.\n"
        "- Don't copy the headline or string job titles together; turn profile text into natural phrases.\n"
        "- No compliments that could apply to anyone, nothing that sounds automated.\n"
        "Reply with the message text only: no preamble, labels or quotes."
    )


MAX_GROWTH = 1.15  # a personalized message may be at most this much longer than the template


def personalize(ai, base: str, contact, brief: str, profile=None) -> str:
    """Have Claude tailor the rendered draft. Returns the draft unchanged on a refusal.

    Models don't count words well, so an over-long result gets one more pass asking
    for a cut; if that's still too long, the shorter of the two is kept.
    """
    prompt = personalization_prompt(base, contact, brief, profile)
    text = (ai.generate(prompt) or "").strip()
    if not text:
        return base
    limit = int(len(base.split()) * MAX_GROWTH)
    if len(text.split()) > limit:
        shorter = (ai.generate(
            f"{prompt}\n\nYour previous version is below. It is {len(text.split())} words; the limit is {limit}. "
            f"Rewrite it to at most {limit} words: keep the single best personal detail and the ask, cut the rest.\n\n"
            f"{text}") or "").strip()
        if shorter and len(shorter.split()) < len(text.split()):
            text = shorter
    return text


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
        if not brief:
            raise SystemExit("--ai needs --brief describing the goal of the campaign.")
        client = ai_backend()

    sender = sender_values(args)
    shown = 0
    for i, c in enumerate(contacts, 1):
        try:
            body = render(template, c, sender)
        except KeyError as e:
            raise SystemExit(f"{e.args[0]} - pass it with --var name=value (sender_* placeholders) "
                             "or give it a fallback like {name|default}.")
        base = body
        if client is not None:
            try:
                profile = db.execute("SELECT * FROM profiles WHERE contact_id=?", (c["id"],)).fetchone()
                body = personalize(client, body, c, brief, profile)
            except Exception as e:  # keep the template draft rather than abort the batch
                print(f"  ! AI personalization failed for #{c['id']} ({type(e).__name__}: {e}); using template.")
        if len(body) > LINKEDIN_MAX_CHARS:
            print(f"  ! #{c['id']}: message is {len(body)} chars (LinkedIn limit {LINKEDIN_MAX_CHARS}).")
        if args.dry_run:
            if shown < 3:
                print(f"--- #{c['id']} {c['first_name']} {c['last_name']} ---\n{body}\n")
                shown += 1
            continue
        db.execute("INSERT OR IGNORE INTO messages (campaign_id, contact_id, body, base_body, ai_at, updated_at) "
                   "VALUES (?,?,?,?,?,?)", (camp["id"], c["id"], body, base, now() if client is not None else None, now()))
        if client is not None:
            print(f"  drafted {i}/{len(contacts)}", end="\r")
    if args.dry_run:
        print(f"(dry run) {len(contacts)} message(s) would be drafted; nothing saved.")
        return
    db.commit()
    print(f"Drafted {len(contacts)} message(s) in campaign '{args.campaign}'. "
          f"Review with: outreach.py review {args.campaign}")


# --------------------------------------------------------------------------
# Making messages personal: read each profile, then have Claude rewrite the draft
# --------------------------------------------------------------------------

def enrich_targets(db, args):
    """Contacts to read profiles for: a campaign's waiting messages, or the filters."""
    if getattr(args, "campaign", None):
        ids = [r["contact_id"] for r in campaign_messages(db, args.campaign, "draft")]
        return [db.execute("SELECT * FROM contacts WHERE id=?", (i,)).fetchone() for i in ids]
    return select_contacts(db, args)


def cmd_enrich(db, args, page_factory=None, should_stop=lambda: False, sleep=time.sleep):
    import linkedin_browser as lb
    if page_factory is None:
        def page_factory():
            return lb.browser(args.profile_dir, headless=args.headless, channel=args.channel)
    cutoff = (dt.datetime.now() - dt.timedelta(days=args.max_age_days)).isoformat(timespec="seconds")
    targets = []
    for c in enrich_targets(db, args):
        have = db.execute("SELECT fetched_at FROM profiles WHERE contact_id=?", (c["id"],)).fetchone()
        if have and have[0] >= cutoff and not args.refresh:
            continue
        targets.append(c)
    if not targets:
        print("Every profile is already read (use --refresh to read them again).")
        return 0
    stop = stop_flag()
    print(f"Reading {len(targets)} profile(s) on LinkedIn (3 pages each, paced)...")
    done = 0
    with page_factory() as page:
        for i, c in enumerate(targets, 1):
            if stop.is_set() or should_stop():
                print("Stopped.")
                break
            who = f"{c['first_name']} {c['last_name']}"
            if not c["url"]:
                print(f"  x {who}: no profile URL")
                continue
            if not done:
                lb.ensure_logged_in(page, lb.feed_url_for(c["url"]), cancelled=lambda: stop.is_set() or should_stop())
            try:
                p = lb.read_profile(page, c["url"], sleep=sleep)
            except lb.StopRun as e:
                print(f"STOPPED: {e}")
                break
            except Exception as e:
                print(f"  x {who}: {type(e).__name__}: {e}")
                continue
            db.execute("INSERT OR REPLACE INTO profiles (contact_id, headline, location, about, experience, "
                       "education, fetched_at) VALUES (?,?,?,?,?,?,?)",
                       (c["id"], p["headline"], p["location"], p["about"], p["experience"], p["education"], now()))
            db.commit()
            done += 1
            print(f"  {i}/{len(targets)} {who}: {len(p['about']) + len(p['experience']) + len(p['education'])} "
                  "characters of profile text")
            if i < len(targets):
                sleep(random.uniform(args.min_delay, args.max_delay))
    print(f"Read {done} profile(s).")
    return done


def cmd_personalize(db, args, ai=None, page_factory=None, should_stop=lambda: False, sleep=time.sleep):
    camp = db.execute("SELECT * FROM campaigns WHERE name=?", (args.campaign,)).fetchone()
    if camp is None:
        raise SystemExit(f"No campaign named '{args.campaign}'.")
    brief = (args.brief or camp["brief"] or "").strip()
    if not brief:
        raise SystemExit("Pass --brief 'what this campaign is for' (it's saved on the campaign for next time).")
    if brief != (camp["brief"] or ""):
        db.execute("UPDATE campaigns SET brief=? WHERE id=?", (brief, camp["id"]))
        db.commit()
    msgs = [m for m in campaign_messages(db, args.campaign, "draft") if args.redo or not m["ai_at"]]
    if not msgs:
        print("Nothing to personalize: every waiting message is done (use --redo to do them again).")
        return
    ai = ai or ai_backend()  # fail before touching the browser if Claude isn't reachable
    if args.enrich:
        cmd_enrich(db, args, page_factory=page_factory, should_stop=should_stop, sleep=sleep)
    stop = stop_flag()
    print(f"Personalizing {len(msgs)} message(s) with {ai.name}...")
    done = 0
    for m in msgs:
        if stop.is_set() or should_stop():
            print("Stopped.")
            break
        who = f"{m['first_name']} {m['last_name']}"
        profile = db.execute("SELECT * FROM profiles WHERE contact_id=?", (m["contact_id"],)).fetchone()
        try:  # always start from the template text, so a redo doesn't build on the last rewrite
            body = personalize(ai, m["base_body"] or m["body"], m, brief, profile)
        except Exception as e:  # keep the template draft rather than abort the batch
            print(f"  x {who}: {type(e).__name__}: {e}")
            if "authentication" in type(e).__name__.lower() or "401" in str(e):
                print("STOPPED: Claude rejected the API key. Check ANTHROPIC_API_KEY (an `export` in the "
                      "terminal takes precedence over the .env file).")
                break
            continue
        db.execute("UPDATE messages SET body=?, ai_at=?, updated_at=? WHERE id=?", (body, now(), now(), m["id"]))
        db.commit()
        done += 1
        print(f"  ok {who}" + ("" if profile else " (profile not read yet: used name and headline only)"))
    print(f"Personalized {done} of {len(msgs)} message(s). Read them before sending: "
          f"outreach.py review {args.campaign}")


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


def cmd_autosend(db, args, sender_factory=None, sleep=time.sleep, prompt=input, cancelled=lambda: False):
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
        first_url = next((m["url"] for m in batch if m["url"]), None)
        if first_url:  # logged out? wait for you to log in before touching the queue
            lb.ensure_logged_in(page, lb.feed_url_for(first_url), cancelled=cancelled)
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

    s = sub.add_parser("login", help="log in to LinkedIn in the automation browser (import-linkedin and "
                                     "autosend also ask when needed)")
    browser_args(s)
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("import-linkedin", help="import connections by reading your LinkedIn connections page "
                                                "(after `login`; the CSV export has more detail)")
    browser_args(s)
    s.add_argument("--max", type=int, help="stop after this many connections")
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.add_argument("--url", default=None, help=argparse.SUPPRESS)  # for tests
    s.set_defaults(func=cmd_import_linkedin)

    def pacing_args(sp, lo, hi):
        sp.add_argument("--min-delay", type=float, default=lo, help=f"min seconds between people (default {lo:.0f})")
        sp.add_argument("--max-delay", type=float, default=hi, help=f"max seconds between people (default {hi:.0f})")

    s = sub.add_parser("enrich", help="read About/Experience/Education from LinkedIn profiles (for personalize)")
    s.add_argument("--campaign", help="the people with waiting messages in this campaign (else: the filters)")
    add_filter_args(s)
    s.add_argument("--max-age-days", type=int, default=30, help="skip profiles read within N days (default 30)")
    s.add_argument("--refresh", action="store_true", help="read profiles again even if recent")
    browser_args(s)
    pacing_args(s, 3, 6)
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.set_defaults(func=cmd_enrich)

    s = sub.add_parser("personalize", help="have Claude rewrite a campaign's waiting drafts for each person")
    s.add_argument("campaign")
    s.add_argument("--brief", help="what this campaign is for (saved on the campaign)")
    s.add_argument("--enrich", action="store_true", help="read profiles first (opens the browser)")
    s.add_argument("--redo", action="store_true", help="also redo messages already personalized")
    s.add_argument("--max-age-days", type=int, default=30, help=argparse.SUPPRESS)
    s.add_argument("--refresh", action="store_true", help=argparse.SUPPRESS)
    browser_args(s)
    pacing_args(s, 3, 6)
    s.add_argument("--headless", action="store_true", help="hide the browser window")
    s.set_defaults(func=cmd_personalize)

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
