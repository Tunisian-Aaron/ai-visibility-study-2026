"""Browser automation for sending LinkedIn messages from your own logged-in session.

Drives a real Chromium/Chrome window with Playwright, using a persistent
browser profile that you log in to by hand once (`outreach.py login`). It
never sees or stores your password.

LinkedIn changes its markup from time to time. Every selector the tool relies
on is in the SELECTORS block below, so a breakage is a one-place fix. When a
step can't find what it needs, the send fails with a screenshot rather than
guessing.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

DEFAULT_PROFILE_DIR = os.environ.get(
    "OUTREACH_BROWSER_PROFILE", str(Path.home() / ".outreach-linkedin-profile"))

# --- selectors (tried in order; first visible match wins) -------------------
# "Message" on a profile is a link to /messaging/compose/?recipient=... (followed directly:
# clicking it opens nothing in an automated window) or, in older layouts, a button that opens
# an overlay. "Message with Premium" is InMail for people you're not connected to: never that.
MESSAGE_BUTTON = [
    ("role", "link", r"^message\b(?!.*premium)"),
    ("role", "button", r"^message\b(?!.*premium)"),
    ("css", "a[href*='/messaging/compose']:not([aria-label*='Premium']), button[aria-label^='Message']"),
]
DEGREE_BADGE = re.compile(r"\b(1st|2nd|3rd)\b")  # next to the name: "· 1st"

# Profile pages (read by `enrich`). The layout has no stable ids or class names, so sections
# are cut out of the page text by their headings. Experience and education aren't on the
# profile page at all any more; they're on /details/experience/ and /details/education/.
PROFILE_HEADINGS = ("About", "Activity", "Experience", "Education", "Skills", "Interests", "Featured",
                    "Highlights", "Licenses & certifications", "Projects", "Publications", "Languages",
                    "Volunteering", "Honors & awards", "Courses", "Recommendations", "Services")
PROFILE_END = ("More profiles for you", "People you may know", "People also viewed", "You might like",
               "Explore Premium profiles", "Ad Options", "Promoted")
EXPAND_TEXT = re.compile(r"^(…\s*)?(see |show )?more$", re.I)  # the "…more" that truncates text
COMPOSER = [
    ("role", "textbox", r"write a message"),
    ("css", "div.msg-form__contenteditable[contenteditable='true']"),
]
SEND_BUTTON = [
    ("css", "button.msg-form__send-button"),
    ("role", "button", r"^send$"),
]
CLOSE_OVERLAY = "button[aria-label*='Close your conversation'], button[aria-label*='Close your draft']"

# Connections list (import-linkedin). The page loads 10 people at a time through a
# "pager" request whose body is fixed apart from startIndex, so once we've seen the page
# make one, we can ask for any slice ourselves without rendering thousands of cards
# (which is what made the browser crawl and then crash past ~1,000). The response is a
# React Server Components stream; each person is tagged like this inside it:
PAGER_URL = re.compile(r"rsc-action/actions/pagination\?.*connectionsList")
PAGER_START = re.compile(r'"startIndex":\d+')
PAGER_NAME = re.compile(r'"id":"profile_name_loading_state"\}\},"namespace":"LoadingNamespace"\},'
                        r'"value":\{"\$case":"stringValue","stringValue":"((?:[^"\\]|\\.)*)"')
PAGER_HEADLINE = re.compile(r'"id":"profile_headline_loading_state"\}\},"namespace":"LoadingNamespace"\},'
                            r'"value":\{"\$case":"stringValue","stringValue":"((?:[^"\\]|\\.)*)"')
PAGER_SLUG = re.compile(r'"vanityName":"([^"]+)"')
PAGER_DATE = re.compile(r'"children":\["Connected on ([^"\]]+)"\]')
PAGER_PAGE_SIZE = 10

# Fallback when the pager can't be captured: scroll the page. Each person is the largest
# element around a profile link that doesn't also contain a link to someone else.
CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
PROFILE_LINK = "main a[href*='/in/']"
SHOW_MORE = [("role", "button", r"^(show more results|load more)\b")]
CARD_NOISE = re.compile(r"^(message|remove connection|more actions|connected on\b|status is\b|view .*profile$|"
                        r"·|\d+(st|nd|rd)\b|1st degree connection)", re.I)

# Signs LinkedIn wants you to stop: login walls, security checks, restrictions.
BLOCK_PATH = re.compile(r"^/(checkpoint|authwall|login|uas/login)")
BLOCK_TEXT = (
    "unusual activity",
    "temporarily restricted",
    "account has been restricted",
    "quick security check",
)


class StopRun(Exception):
    """LinkedIn showed a login wall, security check or restriction notice: stop everything."""


class SendError(Exception):
    """This one message couldn't be sent; the run can continue."""


@contextlib.contextmanager
def browser(profile_dir: str = DEFAULT_PROFILE_DIR, headless: bool = False, channel: str | None = None):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("Automated sending needs Playwright:\n"
                         "  pip install playwright && playwright install chromium")
    opts = {"headless": headless, "viewport": {"width": 1280, "height": 900}}
    if channel:
        opts["channel"] = channel
    elif os.environ.get("OUTREACH_BROWSER_PATH"):
        opts["executable_path"] = os.environ["OUTREACH_BROWSER_PATH"]
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(profile_dir, **opts)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield page
        finally:
            ctx.close()


class LinkedInSender:
    def __init__(self, page, timeout_s: float = 15, confirm_timeout_s: float = 10):
        self.page = page
        self.timeout_s = timeout_s
        self.confirm_timeout_s = confirm_timeout_s

    # -- helpers -------------------------------------------------------------
    def _locators(self, specs):
        for spec in specs:
            if spec[0] == "role":
                yield self.page.get_by_role(spec[1], name=re.compile(spec[2], re.I))
            else:
                yield self.page.locator(spec[1])

    def _first_visible(self, specs, what: str, timeout_s: float | None = None):
        deadline = time.monotonic() + (timeout_s or self.timeout_s)
        while time.monotonic() < deadline:
            for loc in self._locators(specs):
                for i in range(min(loc.count(), 5)):
                    item = loc.nth(i)
                    if item.is_visible():
                        return item
            time.sleep(0.25)
        raise SendError(f"couldn't find the {what}")

    def _clear_focused(self):
        self.page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        self.page.keyboard.press("Delete")

    def _close_overlays(self):
        with contextlib.suppress(Exception):
            loc = self.page.locator(CLOSE_OVERLAY)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    loc.nth(i).click(timeout=2000)

    def _check_first_degree(self):
        """Fail early on a 2nd/3rd-degree profile: its Message link would be a paid InMail."""
        degrees = set()
        with contextlib.suppress(Exception):  # no badge found: let the Message lookup decide
            degrees = set(DEGREE_BADGE.findall(top_card(main_lines(self.page))["degree"]))
        if degrees and "1st" not in degrees:
            raise SendError(f"not a 1st-degree connection ({', '.join(sorted(degrees))})")

    # -- public --------------------------------------------------------------
    def check_blocked(self):
        if BLOCK_PATH.search(urlparse(self.page.url).path or ""):
            raise StopRun(f"LinkedIn redirected to {self.page.url} - log in again with "
                          "`outreach.py login` and complete any verification.")
        try:
            text = self.page.locator("body").inner_text(timeout=5000)[:50000].lower()
        except Exception:
            return
        for phrase in BLOCK_TEXT:
            if phrase in text:
                raise StopRun(f"LinkedIn is showing a '{phrase}' notice - stopping. Check your "
                              "account in the browser before sending more.")

    def send(self, profile_url: str, body: str, dry_run: bool = False):
        if not profile_url:
            raise SendError("contact has no profile URL")
        self.page.goto(profile_url, wait_until="domcontentloaded")
        self.check_blocked()
        self._close_overlays()
        self._check_first_degree()

        btn = self._first_visible(MESSAGE_BUTTON, "Message button (not a 1st-degree connection, "
                                  "or messaging is off)")
        href = btn.get_attribute("href") or ""
        if "/messaging/" in href:
            self.page.goto(urljoin(self.page.url, href), wait_until="domcontentloaded")
            self.check_blocked()
        else:
            btn.click()
        box = self._first_visible(COMPOSER, "message box")
        box.click()
        self._clear_focused()
        # Type line breaks as Shift+Enter, like a person: it never triggers LinkedIn's
        # Enter-to-send, and keeps blank lines exact (inserting "\n" adds extra ones).
        for i, line in enumerate(body.split("\n")):
            if i:
                self.page.keyboard.press("Shift+Enter")
            if line:
                self.page.keyboard.insert_text(line)

        if dry_run:
            self._clear_focused()
            self._close_overlays()
            return

        send_btn = self._first_visible(SEND_BUTTON, "Send button")
        if not send_btn.is_enabled():
            raise SendError("Send button is disabled")
        send_btn.click()

        # LinkedIn empties the composer once the message is accepted.
        deadline = time.monotonic() + self.confirm_timeout_s
        while time.monotonic() < deadline:
            if not box.inner_text().strip():
                self.check_blocked()
                self._close_overlays()
                return
            time.sleep(0.25)
        raise SendError("clicked Send but couldn't confirm the message went out - check the "
                        "conversation in LinkedIn before retrying")

    def screenshot(self, path: str):
        with contextlib.suppress(Exception):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=path, full_page=False)


# --- reading a profile ---------------------------------------------------------

def main_lines(page) -> list[str]:
    text = page.locator("main").inner_text(timeout=10000)
    return [l.strip() for l in text.splitlines() if l.strip()]


def top_card(lines: list[str]) -> dict:
    """Name, degree badge, headline and location from the lines at the top of a profile."""
    head = lines[:8]
    badge = next((i for i, l in enumerate(head) if len(l) < 12 and DEGREE_BADGE.search(l)), None)
    if badge is None:
        return {"name": head[0] if head else "", "degree": "", "headline": "", "location": ""}
    rest = [l for l in head[badge + 1:] if l not in ("·", "Contact info")]
    return {"name": head[0] if badge else "", "degree": head[badge],
            "headline": rest[0] if rest else "", "location": rest[1] if len(rest) > 1 else ""}


def section_text(lines: list[str], heading: str, limit: int) -> str:
    """The text under `heading`, up to the next heading. Only the page's own content counts:
    nothing after the sidebar/footer (whose links include another "About")."""
    end = next((i for i, l in enumerate(lines) if l in PROFILE_END), len(lines))
    lines = lines[:end]
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return ""
    out = []
    for l in lines[start:]:
        if l in PROFILE_HEADINGS:
            break
        if not EXPAND_TEXT.match(l):
            out.append(l)
    return "\n".join(out)[:limit]


def _wait_for_text(page, ready, timeout_s: float = 12) -> list[str]:
    """Poll the page text until ready(lines) is true (profiles render after load), then return it."""
    deadline = time.monotonic() + timeout_s
    lines: list[str] = []
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            lines = main_lines(page)
            if ready(lines):
                break
        page.wait_for_timeout(500)
    return lines


def _expand_text(page):
    """Click the '…more' buttons that truncate About and job descriptions."""
    with contextlib.suppress(Exception):
        page.evaluate("""() => { for (const b of document.querySelectorAll('main button')) {
            if (/^(…\\s*)?(see |show )?more$/i.test((b.innerText || '').trim())) b.click(); } }""")
        page.wait_for_timeout(400)


def read_profile(page, url: str, sleep=time.sleep, pause=(2.0, 4.0)) -> dict:
    """Read one person's profile: headline, location, About, Experience and Education.

    Three page loads, paced by `pause` seconds. Raises StopRun on a login wall or
    security notice, like sending does.
    """
    sender = LinkedInSender(page)
    url = url.rstrip("/")
    page.goto(url, wait_until="domcontentloaded")
    sender.check_blocked()
    # The top card renders first and the sections stream in after it; wait for both.
    def rendered(ls):
        end = next((i for i, l in enumerate(ls) if l in PROFILE_END), len(ls))
        return bool(top_card(ls)["degree"]) and any(h in ls[:end] for h in ("About", "Activity", "Experience", "Education"))
    _wait_for_text(page, rendered)
    _expand_text(page)
    lines = main_lines(page)
    card = top_card(lines)
    out = {"headline": card["headline"], "location": card["location"],
           "about": section_text(lines, "About", 2000)}
    for part, limit in (("experience", 3000), ("education", 1500)):
        sleep(random.uniform(*pause))
        page.goto(f"{url}/details/{part}/", wait_until="domcontentloaded")
        sender.check_blocked()
        _wait_for_text(page, lambda ls: part.capitalize() in ls)
        _expand_text(page)
        out[part] = section_text(main_lines(page), part.capitalize(), limit)
    return out


LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"


def current_url(page) -> str:
    # Ask the browser rather than trusting page.url: Playwright's sync API only learns about
    # navigations the user makes while Python is talking to it, so page.url can be stale.
    with contextlib.suppress(Exception):
        return page.evaluate("location.href")
    return page.url


def logged_in(page) -> bool:
    return not BLOCK_PATH.search(urlparse(current_url(page)).path or "")


def wait_for_login(page, cancelled=lambda: False, timeout_s=900) -> bool:
    """Wait until you've got past LinkedIn's login/verification pages in the browser window."""
    print("LinkedIn is asking you to log in: do it in the browser window (including any 2FA). "
          "This carries on by itself once you're in.")
    deadline = time.monotonic() + timeout_s
    while not logged_in(page):
        if cancelled() or time.monotonic() > deadline:
            return False
        page.wait_for_timeout(1000)
    return True


def ensure_logged_in(page, url: str, cancelled=lambda: False):
    """Open url. If LinkedIn shows a login wall instead, wait for you to log in, then retry."""
    for _ in range(3):
        page.goto(url, wait_until="domcontentloaded")
        if logged_in(page):
            return
        if not wait_for_login(page, cancelled):
            raise StopRun("Not logged in - log in to LinkedIn in the browser window and try again.")
        page.wait_for_timeout(1000)
    raise StopRun(f"LinkedIn keeps redirecting to {page.url} - check your account in the browser window.")


def feed_url_for(url: str) -> str:
    """The feed on the same site as url (so tests against a local mock never touch LinkedIn)."""
    return urlparse(url)._replace(path="/feed/", query="", fragment="").geturl()


# Returns [{url, name, lines}] for every person on the page not already in `seen`,
# keyed by their /in/ URL, and scrolls to the bottom so LinkedIn loads the next batch.
_CARDS_JS = r"""([sel, seen]) => {
  const norm = h => { try { const u = new URL(h, location.href), m = u.pathname.match(/^\/in\/([^/]+)/);
                            return m ? u.origin + '/in/' + m[1] : null; } catch (e) { return null; } };
  const skip = new Set(seen), out = new Map(), links = document.querySelectorAll(sel);
  for (const a of links) {
    const url = norm(a.getAttribute('href'));
    if (!url || skip.has(url) || out.has(url)) continue;
    let card = a;
    while (card.parentElement && card.parentElement !== document.body) {
      const urls = new Set([...card.parentElement.querySelectorAll('a[href*="/in/"]')]
        .map(x => norm(x.getAttribute('href'))).filter(Boolean));
      if (urls.size > 1) break;
      card = card.parentElement;
    }
    const names = [...card.querySelectorAll('a[href*="/in/"]')]
      .filter(x => norm(x.getAttribute('href')) === url)
      .map(x => (x.innerText || '').trim().split('\n')[0].trim()).filter(Boolean);
    out.set(url, {url, name: names[0] || '',
                  lines: (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean)});
    card.dataset.outreachRead = '1';
  }
  // Keep a long list light so the page doesn't die: drop photos of cards already read and
  // hide all but the newest ones (the list end stays visible, so LinkedIn keeps loading more).
  const read = document.querySelectorAll('[data-outreach-read]');
  for (const img of document.querySelectorAll('[data-outreach-read] img')) { img.removeAttribute('src'); img.removeAttribute('srcset'); }
  for (let i = 0; i < read.length - 40; i++) read[i].style.display = 'none';
  if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
  window.scrollTo(0, document.body.scrollHeight);
  return [...out.values()];
}"""


def split_headline(headline: str) -> tuple[str, str]:
    """'Head of Growth at Acme | ex-Google' -> ('Head of Growth', 'Acme')."""
    m = re.match(r"(.+?)(?:\s+at\s+|\s*@\s*)(.+)$", headline)
    if not m:
        return headline, ""
    return m.group(1).strip(), re.split(r"\s+[|·•]\s+", m.group(2))[0].strip()


def parse_card(card: dict) -> dict | None:
    lines = [l for l in card["lines"] if not CARD_NOISE.match(l)]
    name = card["name"] if card["name"] and not CARD_NOISE.match(card["name"]) else (lines[0] if lines else "")
    name = name.split(",")[0].strip()  # "Jane Doe, PhD"
    if not name:
        return None
    headline = next((l for l in lines if not l.startswith(name)), "")
    connected = next((l[len("connected on"):].strip() for l in card["lines"]
                      if l.lower().startswith("connected on")), "")
    position, company = split_headline(headline)
    first, _, last = name.partition(" ")
    return {"url": card["url"], "first_name": first, "last_name": last, "headline": headline,
            "position": position, "company": company, "connected_on": connected}


def _json_string(s: str) -> str:
    with contextlib.suppress(ValueError):
        return json.loads(f'"{s}"')
    return s


def parse_pager_response(text: str, origin: str = "https://www.linkedin.com") -> list[dict]:
    """People in one pager response, in page order, in the same shape as parse_card()."""
    names = [(m.start(), _json_string(m.group(1))) for m in PAGER_NAME.finditer(text)]
    heads = [(m.start(), _json_string(m.group(1))) for m in PAGER_HEADLINE.finditer(text)]
    slugs = [(m.start(), m.group(1)) for m in PAGER_SLUG.finditer(text)]
    dates = [m.group(1) for m in PAGER_DATE.finditer(text)]

    def after(items, pos, limit):
        return next((v for p, v in items if pos < p < limit), "")
    people, seen = [], set()
    for i, (pos, name) in enumerate(names):
        limit = names[i + 1][0] if i + 1 < len(names) else len(text)
        slug = after(slugs, pos, limit)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        headline = after(heads, pos, limit)
        position, company = split_headline(headline)
        first, _, last = name.split(",")[0].strip().partition(" ")
        people.append({"url": f"{origin}/in/{slug}", "first_name": first, "last_name": last, "headline": headline,
                       "position": position, "company": company, "connected_on": ""})
    for p, d in zip(people, dates):  # dates come first in the stream, in the same order as the cards
        p["connected_on"] = d
    return people


def capture_pager(page, timeout_s: float = 12) -> dict | None:
    """Scroll once and catch the page's own pager request: its URL, headers and body are
    the template for asking for any slice. None if the page didn't make one."""
    caught: list = []
    handler = lambda r: caught.append(r) if PAGER_URL.search(r.url) and r.post_data else None  # noqa: E731
    page.on("request", handler)
    try:
        deadline = time.monotonic() + timeout_s
        while not caught and time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                page.evaluate("() => { const l = document.querySelectorAll('main a[href*=\"/in/\"]');"
                              " if (l.length) l[l.length - 1].scrollIntoView({block: 'end'});"
                              " window.scrollTo(0, document.body.scrollHeight); }")
            page.wait_for_timeout(500)
    finally:
        page.remove_listener("request", handler)
    if not caught:
        return None
    r = caught[0]
    headers = {k: v for k, v in r.headers.items()
               if not k.startswith(":") and k.lower() not in ("content-length", "host", "cookie", "accept-encoding")}
    return {"url": r.url, "headers": headers, "body": r.post_data}


def fetch_pager(page, template: dict, start: int):
    """Ask for the slice starting at `start` (10 people). Returns (status, text, final url)."""
    body = PAGER_START.sub(f'"startIndex":{start}', template["body"])
    resp = page.request.post(template["url"], headers=template["headers"], data=body)
    return resp.status, resp.text(), resp.url


def collect_connections(page, url: str = CONNECTIONS_URL, max_contacts: int | None = None,
                        pause=(1.0, 2.0), sleep=time.sleep, should_stop=lambda: False, on_batch=lambda batch: None,
                        stall_s: float = 90, pager_timeout_s: float = 12):
    """Read your connections list, calling on_batch(list of person dicts) as each new
    batch arrives, and returning everyone found.

    Uses the page's own pager request (10 people per call, nothing rendered), or falls
    back to scrolling the page if that request can't be captured. Read-only: it never
    clicks anything except "Show more results". If LinkedIn asks for a login first,
    waits for you to log in. Stops early when should_stop() is true, and raises StopRun
    on a security notice. Because batches are handed over as they're read, the caller
    keeps everything collected even if this raises.
    """
    sender = LinkedInSender(page)
    found: dict = {}
    ensure_logged_in(page, url, cancelled=should_stop)
    sender.check_blocked()
    total = None  # the page header says "1,234 Connections"
    with contextlib.suppress(Exception):
        m = page.locator("main").inner_text(timeout=5000)
        m = re.search(r"([\d,.]+)\s+connections?\b", m, re.I)
        total = int(re.sub(r"\D", "", m.group(1))) if m else None
    of = f" of {total}" if total else ""

    def take(people) -> list:
        batch = []
        for c in people:
            if c["url"] not in found and (not max_contacts or len(found) < max_contacts):
                found[c["url"]] = c
                batch.append(c)
        if batch:
            on_batch(batch)
        return batch

    def done() -> bool:
        return bool((max_contacts and len(found) >= max_contacts) or (total and len(found) >= total))

    template = capture_pager(page, pager_timeout_s)
    if template:
        print(f"Reading{of} connections, 10 per request...")
        origin = urlparse(url)._replace(path="", query="", fragment="").geturl()
        start, retries, since_print = 0, 0, 0
        while not should_stop() and not done():
            status, text, final_url = fetch_pager(page, template, start)
            if status == 200 and BLOCK_PATH.search(urlparse(final_url).path or ""):
                raise StopRun(f"LinkedIn redirected to {final_url} - log in again and complete any verification.")
            if status in (429, 500, 502, 503, 504) and retries < 3:
                retries += 1
                print(f"  LinkedIn answered {status}; waiting a minute before trying again ({retries}/3)...")
                sleep(60)
                continue
            if status != 200:
                print(f"  LinkedIn answered {status} at {len(found)}{of}; stopping here. Run again later to continue.")
                break
            retries = 0
            people = parse_pager_response(text, origin)
            if not people:
                break  # past the end of the list
            take(people)
            start += PAGER_PAGE_SIZE
            since_print += 1
            if since_print >= 5 or done():
                since_print = 0
                print(f"  {len(found)}{of} connections so far...")
            sleep(random.uniform(*pause))
        if not found:
            raise SendError("the connections pager returned nobody - LinkedIn's response format may have changed "
                            "(see PAGER_* at the top of linkedin_browser.py)")
        return list(found.values())

    print(f"Reading{of} connections by scrolling the page (the pager request wasn't seen)...")
    last_new = time.monotonic()
    while time.monotonic() - last_new < stall_s and not should_stop():
        before = len(found)
        try:
            cards = page.evaluate(_CARDS_JS, [PROFILE_LINK, list(found)])
        except Exception:  # the page reloaded or navigated mid-scan: check where it went, then retry
            sleep(2)
            sender.check_blocked()
            continue
        if take([c for c in map(parse_card, cards) if c]):
            last_new = time.monotonic()
            print(f"  {len(found)}{of} connections so far...")
        if done():
            break
        with contextlib.suppress(Exception):
            page.mouse.wheel(0, 2500)
            for loc in sender._locators(SHOW_MORE):
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    break
        sleep(random.uniform(*pause))
        sender.check_blocked()
    if not found:
        raise SendError("found no connections on the page - LinkedIn's layout may have changed "
                        "(selectors are at the top of linkedin_browser.py)")
    return list(found.values())


def interactive_login(profile_dir: str, headless: bool = False, channel: str | None = None,
                      cancelled=lambda: False, login_url: str = LOGIN_URL):
    """Open LinkedIn's login page and wait until you've logged in (detected automatically)."""
    with browser(profile_dir, headless=headless, channel=channel) as page:
        page.goto(login_url, wait_until="domcontentloaded")
        ok = logged_in(page) or wait_for_login(page, cancelled)  # /login redirects to the feed when logged in
        if ok:
            page.goto(feed_url_for(login_url), wait_until="domcontentloaded")
        if not ok or not logged_in(page):
            raise SystemExit("Still not logged in - run `outreach.py login` again.")
        print(f"Logged in. Session saved in {profile_dir} (keep this folder private).")
