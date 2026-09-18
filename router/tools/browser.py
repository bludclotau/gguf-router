"""Stateful Playwright browser: one context per persona, cookies persist.

Judgment call: keep a single Chromium process for the router lifetime and
one BrowserContext per persona (wendy/gumbo/tabatha). storageState is
written to data/browser/<persona>.json and mirrored into tools.tool_state
so a restart still comes back logged-in.
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

GOTO_TIMEOUT_MS = 45000
ACTION_TIMEOUT_MS = 8000
READ_CHAR_CAP = 4000

BLOCKED_TITLE = re.compile(
    r"just a moment|attention required|access denied|verify you are human|"
    r"captcha|checking your browser|unusual traffic|are you a robot",
    re.I,
)
BLOCKED_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare']",
    "#challenge-form",
    ".cf-browser-verification",
    "input[name='cf-turnstile-response']",
)


_lock = threading.Lock()
_pw = None
_browser = None
_contexts = {}
_pages = {}


def _state_path(persona: str) -> Path:
    safe = re.sub(r"[^a-z0-9_-]+", "_", (persona or "wendy").lower())
    return STATE_DIR / f"{safe}.json"


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
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return db.load_browser_state(persona)


def _save_storage(persona: str, context) -> None:
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


def _ensure_page(persona: str):
    _ensure_browser()
    persona = (persona or "wendy").lower()
    if persona in _pages:
        return _pages[persona]
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
        for persona, context in list(_contexts.items()):
            try:
                _save_storage(persona, context)
            except Exception:
                pass
            try:
                context.close()
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


def detect_block(page) -> dict | None:
    try:
        title = page.title() or ""
        url = page.url or ""
    except Exception:
        return {"status": "blocked", "reason": "page_unreadable"}

    if BLOCKED_TITLE.search(title) or BLOCKED_TITLE.search(url):
        return {
            "status": "blocked",
            "reason": "challenge",
            "title": title,
            "url": url,
        }

    for sel in BLOCKED_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return {
                    "status": "blocked",
                    "reason": "captcha",
                    "selector": sel,
                    "title": title,
                    "url": url,
                }
        except Exception:
            continue

    # Login wall on a read/goto — not during type/submit, which may be filling it.
    try:
        pw_count = page.locator("input[type='password']").count()
        loginish = page.get_by_text(re.compile(r"log\s*in|sign\s*in", re.I)).count()
        if pw_count > 0 and loginish > 0:
            return {
                "status": "blocked",
                "reason": "login_wall",
                "title": title,
                "url": url,
            }
    except Exception:
        pass
    return None


def _snapshot(page, extra=None, check_block=True) -> dict:
    try:
        title = page.title()
        url = page.url
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    result = {"status": "ok", "url": url, "title": title}
    if extra:
        result.update(extra)
    if check_block:
        blocked = detect_block(page)
        if blocked:
            blocked.update({k: v for k, v in result.items() if k not in blocked})
            blocked["status"] = "blocked"
            return blocked
    return result


def browser_goto(url: str, persona: str = "wendy", **_):
    if not url:
        return {"status": "error", "error": "url is required"}
    with _lock:
        try:
            page = _ensure_page(persona)
            page.goto(url, timeout=GOTO_TIMEOUT_MS, wait_until="domcontentloaded")
            _save_storage(persona, _contexts[persona.lower()])
            return _snapshot(page)
        except PlaywrightTimeout:
            return {"status": "blocked", "reason": "timeout", "url": url}
        except Exception as exc:
            return {"status": "error", "error": str(exc), "url": url}


def browser_read(persona: str = "wendy", **_):
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
            return snap
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


def browser_click(selector: str, persona: str = "wendy", **_):
    if not selector:
        return {"status": "error", "error": "selector is required"}
    with _lock:
        try:
            page = _ensure_page(persona)
            _locator(page, selector).first.click(timeout=ACTION_TIMEOUT_MS)
            page.wait_for_timeout(300)
            _save_storage(persona, _contexts[persona.lower()])
            return _snapshot(page, extra={"clicked": selector})
        except PlaywrightTimeout:
            return {"status": "error", "error": "click_timeout", "selector": selector}
        except Exception as exc:
            return {"status": "error", "error": str(exc), "selector": selector}


def browser_type(selector: str, text: str = "", persona: str = "wendy", **_):
    if not selector:
        return {"status": "error", "error": "selector is required"}
    with _lock:
        try:
            page = _ensure_page(persona)
            loc = _locator(page, selector).first
            loc.click(timeout=ACTION_TIMEOUT_MS)
            loc.fill(str(text))
            _save_storage(persona, _contexts[persona.lower()])
            return _snapshot(page, extra={"typed": True, "selector": selector}, check_block=False)
        except Exception as exc:
            return {"status": "error", "error": str(exc), "selector": selector}


def browser_submit(selector: str | None = None, persona: str = "wendy", **_):
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
            _save_storage(persona, _contexts[persona.lower()])
            return _snapshot(page, extra={"submitted": selector or "Enter"})
        except Exception as exc:
            return {"status": "error", "error": str(exc), "selector": selector}
