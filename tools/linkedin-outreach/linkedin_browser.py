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
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_PROFILE_DIR = os.environ.get(
    "OUTREACH_BROWSER_PROFILE", str(Path.home() / ".outreach-linkedin-profile"))

# --- selectors (tried in order; first visible match wins) -------------------
MESSAGE_BUTTON = [
    ("role", "button", r"^message\b"),
    ("role", "link", r"^message\b"),
    ("css", "a[href*='/messaging/compose'], button[aria-label^='Message']"),
]
COMPOSER = [
    ("role", "textbox", r"write a message"),
    ("css", "div.msg-form__contenteditable[contenteditable='true']"),
]
SEND_BUTTON = [
    ("css", "button.msg-form__send-button"),
    ("role", "button", r"^send$"),
]
CLOSE_OVERLAY = "button[aria-label*='Close your conversation'], button[aria-label*='Close your draft']"

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

        self._first_visible(MESSAGE_BUTTON, "Message button (not a 1st-degree connection, "
                            "or messaging is off)").click()
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


def interactive_login(profile_dir: str, headless: bool = False, channel: str | None = None, prompt=input):
    with browser(profile_dir, headless=headless, channel=channel) as page:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        prompt("Log in to LinkedIn in the browser window (including any 2FA), "
               "then press Enter here... ")
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        if BLOCK_PATH.search(urlparse(page.url).path or ""):
            raise SystemExit("Still not logged in - run `outreach.py login` again.")
        print(f"Logged in. Session saved in {profile_dir} (keep this folder private).")
