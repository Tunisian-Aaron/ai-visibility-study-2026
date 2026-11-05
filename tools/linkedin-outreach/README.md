# LinkedIn outreach assistant

A command-line tool for messaging your LinkedIn connections, for example to ask
people to try a new product or to open investor conversations. It handles the
tedious parts: importing your connections, picking who to contact, drafting a
personal message for each person, tracking who you've messaged and who replied,
and moving you through the queue quickly.

There are two ways to send:

- **`autosend`, fully automated.** Drives a browser that's logged in to your
  LinkedIn account. For each message it opens the profile, clicks *Message*,
  types the text and clicks *Send*, then waits 1-3 minutes before the next one.
  It stops at a daily cap.
- **`send`, assisted.** Copies each message to your clipboard and opens the
  profile, and you press Send yourself (about 10 seconds a person).

> **Account risk with `autosend`:** LinkedIn's User Agreement prohibits
> automated messaging. LinkedIn can warn, restrict, or ban accounts it catches.
> The defaults (20 messages a day, 60-180 seconds apart) are conservative.
> Raising them increases the risk. The tool stops immediately if LinkedIn shows
> a login wall, a security check or a restriction notice. It makes no attempt
> to hide that it's automated and doesn't solve CAPTCHAs.

Needs Python 3.9+. `autosend` also needs Playwright
(`pip install playwright && playwright install chromium`), and the optional
`--ai` flag needs `pip install anthropic`.

## Easiest: the web page

```bash
pip install playwright && playwright install chromium   # once
python webapp.py
```

This opens a page in your browser at http://127.0.0.1:8765 with four tabs:

1. **Contacts:** import your connections straight from LinkedIn (or from
   `Connections.csv`) and filter who to message.
2. **Write:** write or pick a template, fill in your details, preview, and
   create the drafts.
3. **Review:** edit or skip individual messages, mark replies, and requeue
   failures.
4. **Send:** do a test run, then **Start sending**. You can watch progress live
   and press Stop at any time.

The first time a browser window opens on LinkedIn's login page, log in there
(the tool never sees your password); everything carries on by itself after that.

The page and everything it stores stay on your computer. The server only
listens on 127.0.0.1, rejects requests coming from other websites, and uses the
same `outreach.db` as the command-line tool, so you can switch between the two.
Keep the terminal window open while it's sending; closing it stops the run.

The rest of this README covers the command-line version.

## 1. Get your connections

LinkedIn → **Settings → Data privacy → Get a copy of your data** → choose
**Connections** → download. You get `Connections.csv` (usually within about 10
minutes). It's gitignored here, so don't commit it.

```bash
python outreach.py import ~/Downloads/Connections.csv
```

The file has names, profile URLs, company, title and connection date. Emails
appear only for people who allow it. Run `import` again any time to refresh;
contacts already in the database are updated in place.

**Without waiting for the export:** read the list straight from your
connections page:

```bash
python outreach.py import-linkedin            # --max 200 to stop early
```

It opens the page in the automation browser (if LinkedIn asks you to log in, do
it in that window and the import carries on by itself), then asks for the list
the same way the page does as you scroll, 10 people per request, about one
request a second, so a few thousand connections take minutes. If that request
can't be seen it falls back to scrolling the page. It never clicks anything
except *Show more results*, stops on a security check, saves as it goes, and
keeps what it found if you Ctrl-C. You get name, profile URL and connection
date. Title and company come from each person's headline ("Head of
Growth at Bloom" → *Head of Growth*, *Bloom*), so they're rougher than the
CSV's. Importing the CSV later overwrites them, while `import-linkedin` only
fills fields that are still empty. This is automated browsing of LinkedIn too,
with the same account risk as `autosend` below.

## 2. Pick an audience

```bash
python outreach.py list --title founder --title "head of" --title cto --since 2023-01-01
python outreach.py list --title partner --title principal --title angel --exclude-company recruit
```

You can repeat `--title` and `--company`; repeated values are OR-ed together.
There are also `--exclude-title`, `--exclude-company`, `--since`, `--before`,
`--ids 4,9,12` and `--limit`. Mark people never to contact with
`python outreach.py dnc 12,40`.

## 3. Draft a campaign

Templates are plain text files. Placeholders are filled from each contact:
`{first_name}`, `{last_name}`, `{company}`, `{position}`. Your own values come
from `{sender_name}` and `{sender_<anything>}`, set with `--var`. Add a fallback
for empty fields like this: `{company|your team}`.

```bash
# Product feedback / beta users
python outreach.py draft beta-oct --template templates/product-beta.txt \
  --sender-name "Sam" --var product="Acme" --var audience="small agencies" \
  --var problem="tracking how AI assistants describe their clients" \
  --title founder --title "head of" --dry-run       # preview 3, save nothing

# Investors
python outreach.py draft seed-round --template templates/investor-intro.txt \
  --sender-name "Sam" --var pitch="We help ..." --var traction="We're at ..." \
  --title partner --title principal --title angel
```

Running `draft` again for the same campaign adds only people who aren't in it
yet. Anyone you've messaged in *any* campaign within the last 30 days is skipped
(change this with `--cooldown-days`).

Review and change drafts before sending:

```bash
python outreach.py review beta-oct
python outreach.py export beta-oct exports/beta-oct.csv     # edit in a spreadsheet...
python outreach.py import-drafts beta-oct exports/beta-oct.csv  # ...and load the edits back
```

### Making each message personal

Template messages read like templates. `personalize` fixes that in two steps:
it reads each person's LinkedIn profile (About, Experience, Education; three
page loads per person, paced), then has Claude rewrite their draft around one
or two real details from it, keeping the structure, the ask, the links and the
sign-off, and using only facts from the profile.

```bash
python outreach.py personalize beta-oct --enrich \
  --brief "Get honest product feedback on Acme from founders, and open the door to a call"
python outreach.py review beta-oct          # read every message before sending
```

`--enrich` opens the browser (log in if asked). Profiles are kept for 30 days
in the database, so running again only reads new people. Messages already
personalized are left alone unless you pass `--redo`. If Claude fails on one
message, that draft keeps its template text and the rest continue.

Claude is reached one of two ways, whichever is available:

- **Claude Code** already installed and logged in on this computer (`claude
  login`). Generation counts toward that plan's usage, no API key needed.
- **The Claude API**, when `ANTHROPIC_API_KEY` is set and `pip install
  anthropic` is done. Billed to that account. Put the key in a `.env` file
  next to `outreach.py` (`ANTHROPIC_API_KEY=sk-ant-...`; the file is
  gitignored) and both the command line and the web page pick it up.

Claude sees the campaign brief, the draft, and the profile text; never emails.
`draft --ai` does the same rewrite at drafting time, using whatever profiles
have been read (`enrich --title founder` reads profiles for a filter).

## 4. Send

### Fully automated: `autosend`

The first time, a browser window opens on LinkedIn's login page: log in
yourself, including 2FA, and the run carries on once you're in. The session is
saved in `~/.outreach-linkedin-profile`. The tool never sees your password. Keep
that folder private, because anyone who has it can use your LinkedIn session.
To log in again on its own (for example after LinkedIn logs you out):

```bash
python outreach.py login                 # add --channel chrome to use your installed Chrome
```

Do a dry run first. It opens each profile and types the message but never
clicks Send:

```bash
python outreach.py autosend beta-oct --dry-run --max 3
```

Then send for real:

```bash
python outreach.py autosend beta-oct     # asks you to type 'send' to confirm
```

It sends up to `--daily-cap` messages (default 20 per rolling 24 hours, across
all campaigns), waits a random 60-180 seconds between messages
(`--min-delay`/`--max-delay`), and marks each one sent as it goes. Stop it any
time with Ctrl-C, and the next run picks up where it left off.

| What happens | Result |
|---|---|
| Login wall, security check, "unusual activity" or restriction notice | Stops the whole run and saves a screenshot |
| No Message button (not 1st-degree, or messaging off) | That message is marked `failed`; the run continues |
| Clicked Send but couldn't confirm it went out | Marked `failed`, never retried automatically, so nobody gets it twice |
| 3 failures in a row | Stops; LinkedIn's layout has probably changed (selectors are at the top of `linkedin_browser.py`) |

Failure screenshots go to `failures/`. Look at failures with
`review beta-oct --status failed`. Requeue them after checking with
`mark beta-oct 12,40 draft`.

**Running it every day without you there:** schedule it with cron or Task
Scheduler, using `--yes` to skip the confirmation prompt. The computer has to
be on, and the saved login has to still be valid. For example, weekdays at 10:15:

```
15 10 * * 1-5  cd /path/to/linkedin-outreach && python outreach.py autosend beta-oct --yes >> autosend.log 2>&1
```

### Assisted: `send`

```bash
python outreach.py send beta-oct
```

The message is copied to your clipboard and the profile opens. In LinkedIn,
click **Message**, paste and send. Then answer in the terminal:

| Key | Action |
|---|---|
| Enter | mark as sent |
| `s` | skip for now |
| `e` | edit the message (uses `$EDITOR` if set) |
| `x` | never contact this person |
| `q` | quit; pick up where you left off next time |

## 5. Track replies

```bash
python outreach.py mark beta-oct 12,40 replied
python outreach.py status
```

`status` shows draft, sent, skipped and replied counts and the reply rate for
each campaign. Your data lives in `outreach.db` (SQLite, gitignored); set
`--db` or `OUTREACH_DB` to keep it somewhere else.

## Before you message investors

In the US, a Reg D 506(b) raise can't use *general solicitation*. Sending an
investment pitch to every connection, including people you have no real prior
relationship with, can count as general solicitation. Other countries have
similar rules. Keep investor campaigns to people you actually know and who are
likely to be accredited. Ask for a conversation rather than pitching the
securities in the first message. Check with your counsel.

## Tests

```bash
python -m unittest discover -s tests
```

`tests/test_autosend.py` runs the real automation in a headless browser against
a local mock of LinkedIn's profile and messaging UI. It's skipped if Playwright
isn't installed. It never contacts LinkedIn.
