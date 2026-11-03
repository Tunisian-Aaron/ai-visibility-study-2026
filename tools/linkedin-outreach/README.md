# LinkedIn outreach assistant

A command-line tool for messaging your LinkedIn connections, for example to ask
people to try a new product or to open investor conversations. It handles the
tedious parts: importing your connections, picking who to contact, drafting a
personal message for each person, tracking who you've messaged and who replied,
and moving you through the queue quickly.

**You press Send yourself.** For each message the tool copies the text to your
clipboard and opens the person's profile. You click *Message*, paste, send, and
press Enter in the terminal. This takes about 10 seconds a person. The tool
doesn't log in to LinkedIn or send for you: LinkedIn's User Agreement prohibits
bots and automated messaging, and it restricts accounts that send in bulk. A
daily cap (25 by default) and a 30-day cooldown keep you under the levels that
trigger those restrictions.

Needs Python 3.9+ and nothing else. `pip install anthropic` is only needed for
the optional `--ai` flag.

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

Template messages read like templates. There are two ways to make each one
specific to the person:

- **In a Claude Code / Claude session, with no API key.** Export the campaign
  and ask Claude to rewrite the `message` column for each row, based on that
  person's title and company. Then load the result back with `import-drafts`.
  This counts toward your Claude plan usage, not API credits.
- **With `--ai` on `draft`.** Each draft is rewritten by the Claude API
  (`claude-opus-5-5`, low effort, with server-side refusal fallback on). This
  needs `pip install anthropic` and an `ANTHROPIC_API_KEY` (or `ant auth login`)
  and is billed to that API account. Also pass `--brief "what this campaign is
  for"`. Claude only sees name, title and company, never email. It is told not
  to invent shared history.

## 4. Send

```bash
python outreach.py send beta-oct
```

For each draft, the message is copied to your clipboard and the profile opens
in your browser. In LinkedIn, click **Message**, paste and send. Then answer in
the terminal:

| Key | Action |
|---|---|
| Enter | mark as sent |
| `s` | skip for now |
| `e` | edit the message (uses `$EDITOR` if set) |
| `x` | never contact this person |
| `q` | quit; pick up where you left off next time |

The loop stops at `--daily-cap` (default 25 a day across all campaigns). New
accounts, or accounts that don't usually send many messages, should stay lower.

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
