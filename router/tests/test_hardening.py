"""No-Playwright unit checks for block classification, tool JSON, agent names."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_loop import parse_plan, parse_tool_call, resolve_agent_name
from tools.browser import classify_block, format_block_message, normalize_persona


def test_normalize_persona():
    assert normalize_persona("Wendy") == "wendy"
    assert normalize_persona("Tech") == "tech"
    assert normalize_persona(None) == "unknown"
    assert normalize_persona("gumbo!!") == "gumbo"


def test_resolve_agent_name():
    assert resolve_agent_name("gumbo", "discord") == "gumbo"
    assert resolve_agent_name(None, "tabatha") == "tabatha"
    assert resolve_agent_name("tech", "wendy") == "tech"
    assert resolve_agent_name("nope", "analysis") == "analysis"


def test_parse_tool_call():
    raw = '{"tool":"browser_goto","args":{"url":"https://example.com"}}'
    parsed = parse_tool_call(raw)
    assert parsed["tool"] == "browser_goto"
    assert parsed["args"]["url"] == "https://example.com"
    wrapped = 'Sure.\n{"tool":"browser_read","args":{}}\n'
    assert parse_tool_call(wrapped)["tool"] == "browser_read"
    assert parse_tool_call("just chatting") is None
    reply = parse_plan('{"reply":"Example Domain is the heading."}')
    assert reply == {"kind": "reply", "reply": "Example Domain is the heading."}
    both = parse_plan('{"tool":"browser_read","args":{}}')
    assert both["kind"] == "tool"


def test_challenge_and_captcha():
    hit = classify_block("Just a moment...", "https://example.com/", "", 0, ())
    assert hit and hit["reason"] == "challenge"
    cap = classify_block("Home", "https://x.test/", "", 0, ("iframe[src*='recaptcha']",))
    assert cap and cap["reason"] == "captcha"
    body = classify_block("Site", "https://x.test/", "Verify you are human to continue", 0, ())
    assert body and body["reason"] == "challenge"


def test_login_wall_not_header_login():
    # Content page with a nav "Log in" link and a hidden/no visible password: not a wall.
    assert classify_block("Example Domain", "https://example.com/", "Log in Sign up Learn more", 0, ()) is None
    wall = classify_block("Log in", "https://example.com/login", "", 1, ())
    assert wall and wall["reason"] == "login_wall"
    wall2 = classify_block("Welcome", "https://example.com/account/login?next=/", "", 1, ())
    assert wall2 and wall2["reason"] == "login_wall"


def test_block_message_is_explicit():
    msg = format_block_message({"reason": "captcha", "url": "https://paywall.test/"})
    assert "CAPTCHA" in msg
    assert "https://paywall.test/" in msg
    login = format_block_message({"reason": "login_wall", "url": "https://x.test/login"})
    assert "login" in login.lower()


if __name__ == "__main__":
    tests = [
        test_normalize_persona,
        test_resolve_agent_name,
        test_parse_tool_call,
        test_challenge_and_captcha,
        test_login_wall_not_header_login,
        test_block_message_is_explicit,
    ]
    for fn in tests:
        fn()
        print("ok", fn.__name__)
    print("all ok")
