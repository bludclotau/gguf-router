"""Stateful Playwright browser: one context per persona, cookies persist.

Judgment call: keep a single Chromium process for the router lifetime and
one BrowserContext per persona. storageState is written to
data/browser/<persona>.json and mirrored into tools.tool_state so a
restart still comes back logged-in. Contexts are never shared across
personas — mixing wendy cookies into gumbo was the first-pass footgun
when persona defaulted to "wendy".
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

import db

BASE_DIR = Path(__file__).resolve().parent.parent.parent
STATE_DIR = BASE_DIR / "data" / "browser"
STATE_DIR.mkdir(parents=True, exist_ok=True)

GOTO_TIMEOUT_MS = 20000
ACTION_TIMEOUT_MS = 8000
CHALLENGE_WAIT_MS = 2000
CHALLENGE_ROUNDS = 4
READ_CHAR_CAP = 4000

BLOCKED_TITLE = re.compile(
    r"just a moment|attention required|access denied|verify you are human|"
    r"captcha|checking your browser|unusual traffic|are you a robot|"
    r"enable javascript and cookies|pardon our interruption|"
    r"blocked|forbidden|request unsuccessful",
    re.I,
)
BLOCKED_BODY = re.compile(
    r"verify you are human|checking your browser before accessing|"
    r"enable javascript and cookies to continue|cf-browser-verification|"
    r"why did this happen|additional security check|select all images|"
    r"i'?m not a robot|hcaptcha|recaptcha",
    re.I,
)
BLOCKED_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare']",
    "iframe[src*='turnstile']",
    "#challenge-form",
    "#challenge-running",
    ".cf-browser-verification",
    ".cf-turnstile",
    "input[name='cf-turnstile-response']",
    "#px-captcha",
    ".px-captcha-container",
    "#datadome",
    "iframe[src*='datadome']",
)
LOGIN_URL = re.compile(
    r"/(login|log-in|signin|sign-in|sign_in|auth/login|account/login|session/new)(/|$|\?)",
    re.I,
)
LOGIN_TITLE = re.compile(r"^\s*(log\s*in|sign\s*in|sign\s*up|create account)\s*$", re.I)

_lock = threading.Lock()
_pw = None
_browser = None
_contexts = {}
_pages = {}


def normalize_persona(persona: str | None) -> str:
    raw = (persona or "unknown").strip().lower()
    raw = re.sub(r"[^a-z0-9_-]+", "_", raw).strip("_")
    return raw or "unknown"


def _state_path(persona: str) -> Path:
    return STATE_DIR / f"{normalize_persona(persona)}.json"


def _ensure_browser():
    global _pw, _browser
    if _browser is not None:
        return
    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=True)


def _load_storage(persona: str):
    path = _state_path(persona)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and ("cookies" in data or "origins" in data):
                return data
        except Exception:
            pass
    state = db.load_browser_state(persona)
    if isinstance(state, dict) and ("cookies" in state or "origins" in state):
        return state
    return None


def _save_storage(persona: str) -> None:
    persona = normalize_persona(persona)
    context = _contexts.get(persona)
    if context is None:
        return
    try:
        state = context.storage_state()
    except Exception:
        return
    path = _state_path(persona)
    path.write_text(json.dumps(state), encoding="utf-8")
    try:
        db.save_browser_state(persona, state)
    except Exception:
        pass


def _page_alive(page) -> bool:
    try:
        _ = page.url
        return True
    except Exception:
        return False


def _ensure_page(persona: str):
    _ensure_browser()
    persona = normalize_persona(persona)
    page = _pages.get(persona)
    if page is not None and _page_alive(page):
        return page
    old = _contexts.pop(persona, None)
    _pages.pop(persona, None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    storage = _load_storage(persona)
    kwargs = {}
    if storage:
        kwargs["storage_state"] = storage
    context = _browser.new_context(**kwargs)
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT_MS)
    _contexts[persona] = context
    _pages[persona] = page
    return page


def shutdown() -> None:
    global _pw, _browser, _contexts, _pages
    with _lock:
        for persona in list(_contexts.keys()):
            try:
                _save_storage(persona)
            except Exception:
                pass
            try:
                _contexts[persona].close()
            except Exception:
                pass
        _contexts = {}
        _pages = {}
        if _browser is not None:
            try:
                _browser.close()
            except Exception:
                pass
            _browser = None
        if _pw is not None:
            try:
                _pw.stop()
            except Exception:
                pass
            _pw = None


def _locator(page, selector: str):
    selector = (selector or "").strip()
    if selector.startswith("text="):
        return page.get_by_text(selector[5:], exact=False)
    if selector.startswith("role="):
        return page.get_by_role(selector[5:])
    return page.locator(selector)


def _clean_text(raw: str) -> str:
    text = re.sub(r"[ \t]+", " ", raw or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:READ_CHAR_CAP]


def classify_block(title: str, url: str, body: str = "", visible_password: int = 0, hits: tuple = ()) -> dict | None:
    """Pure helper so we can unit-test without Playwright."""
    title = title or ""
    url = url or ""
    body = body or ""
    if BLOCKED_TITLE.search(title) or BLOCKED_TITLE.search(url) or BLOCKED_BODY.search(body[:2000]):
        reason = "captcha" if hits else "challenge"
        if hits:
            reason = "captcha"
        return {"status": "blocked", "reason": reason, "title": title, "url": url}
    if hits:
        return {
            "status": "blocked",
            "reason": "captcha",
            "selector": hits[0],
            "title": title,
            "url": url,
        }
    # Login wall: password field *visible* AND this looks like a login page,
    # not a content page with a header "Log in" link (first-pass false positive).
    if visible_password > 0 and (LOGIN_URL.search(url) or LOGIN_TITLE.search(title)):
        return {"status": "blocked", "reason": "login_wall", "title": title, "url": url}
    return None


def detect_block(page) -> dict | None:
    try:
        title = page.title() or ""
        url = page.url or ""
    except Exception:
        return {"status": "blocked", "reason": "page_unreadable"}

    hits = []
    for sel in BLOCKED_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                hits.append(sel)
        except Exception:
            continue

    body = ""
    try:
        body = page.inner_text("body")[:2000]
    except Exception:
        pass

    visible_password = 0
    try:
        visible_password = page.locator("input[type='password']:visible").count()
    except Exception:
        try:
            visible_password = page.locator("input[type='password']").count()
        except Exception:
            visible_password = 0

    return classify_block(title, url, body, visible_password, tuple(hits))


def _wait_out_challenge(page) -> dict | None:
    blocked = detect_block(page)
    if not blocked or blocked.get("reason") not in ("challenge", "captcha"):
        return blocked
    for _ in range(CHALLENGE_ROUNDS):
        page.wait_for_timeout(CHALLENGE_WAIT_MS)
        blocked = detect_block(page)
        if not blocked or blocked.get("reason") not in ("challenge", "captcha"):
            return blocked
    return blocked


def _snapshot(page, extra=None, check_block=True, wait_challenge=False) -> dict:
    try:
        title = page.title()
        url = page.url
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    result = {"status": "ok", "url": url, "title": title}
    if extra:
        result.update(extra)
    if not check_block:
        return result
    blocked = _wait_out_challenge(page) if wait_challenge else detect_block(page)
    if blocked:
        blocked.update({k: v for k, v in result.items() if k not in blocked})
        blocked["status"] = "blocked"
        blocked["message"] = format_block_message(blocked)
        return blocked
    return result


def format_block_message(result: dict) -> str:
    reason = (result or {}).get("reason") or "challenge"
    url = (result or {}).get("url") or "that page"
    title = (result or {}).get("title") or ""
    copy = {
        "captcha": f"I hit a CAPTCHA / bot check on {url} and stopped. I didn't pretend the page loaded.",
        "challenge": f"That page challenged me ({title or 'security check'}) at {url}. I stopped rather than guessing.",
        "login_wall": f"That page wants a login at {url}. I'm not signed in, so I stopped.",
        "timeout": f"The page at {url} didn't load in time.",
        "page_unreadable": "The browser lost the page. I stopped rather than guessing.",
    }
    return copy.get(reason, f"I hit a wall on {url} ({reason}) and stopped.")


def browser_goto(url: str, persona: str = "unknown", **_):
    if not url:
        return {"status": "error", "error": "url is required"}
    persona = normalize_persona(persona)
    with _lock:
        try:
            page = _ensure_page(persona)
            page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            snap = _snapshot(page, wait_challenge=True)
            if snap.get("status") != "blocked":
                _save_storage(persona)
            else:
                snap["message"] = format_block_message(snap)
            return snap
        except PlaywrightTimeout:
            return {
                "status": "blocked",
                "reason": "timeout",
                "url": url,
                "message": format_block_message({"reason": "timeout", "url": url}),
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc), "url": url}


def browser_read(persona: str = "unknown", **_):
    persona = normalize_persona(persona)
    with _lock:
        try:
            page = _ensure_page(persona)
            blocked = detect_block(page)
            body = ""
            try:
                body = page.inner_text("body")
            except Exception:
                body = page.inner_text("html")
            text = _clean_text(body)
            snap = _snapshot(page, extra={"text": text}, check_block=False)
            if blocked:
                snap.update(blocked)
                snap["text"] = text
                snap["message"] = format_block_message(snap)
            return snap
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


def browser_click(selector: str, persona: str = "unknown", **_):
    if not selector:
        return {"status": "error", "error": "selector is required"}
    persona = normalize_persona(persona)
    with _lock:
        try:
            page = _ensure_page(persona)
            _locator(page, selector).first.click(timeout=ACTION_TIMEOUT_MS)
            page.wait_for_timeout(300)
            snap = _snapshot(page, extra={"clicked": selector}, wait_challenge=True)
            if snap.get("status") != "blocked":
                _save_storage(persona)
            return snap
        except PlaywrightTimeout:
            return {"status": "error", "error": "click_timeout", "selector": selector}
        except Exception as exc:
            return {"status": "error", "error": str(exc), "selector": selector}


def browser_type(selector: str, text: str = "", persona: str = "unknown", **_):
    if not selector:
        return {"status": "error", "error": "selector is required"}
    persona = normalize_persona(persona)
    with _lock:
        try:
            page = _ensure_page(persona)
            loc = _locator(page, selector).first
            loc.click(timeout=ACTION_TIMEOUT_MS)
            loc.fill(str(text))
            _save_storage(persona)
            return _snapshot(page, extra={"typed": True, "selector": selector}, check_block=False)
        except Exception as exc:
            return {"status": "error", "error": str(exc), "selector": selector}


def browser_submit(selector: str | None = None, persona: str = "unknown", **_):
    persona = normalize_persona(persona)
    with _lock:
        try:
            page = _ensure_page(persona)
            if selector:
                loc = _locator(page, selector).first
                try:
                    loc.press("Enter")
                except Exception:
                    loc.click(timeout=ACTION_TIMEOUT_MS)
            else:
                page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            snap = _snapshot(page, extra={"submitted": selector or "Enter"}, wait_challenge=True)
            if snap.get("status") != "blocked":
                _save_storage(persona)
            return snap
        except Exception as exc:
            return {"status": "error", "error": str(exc), "selector": selector}
