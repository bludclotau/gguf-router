"""Bounded propose → tool → re-prompt loop.

The planner is a separate, grammar-constrained llama.cpp call (GBNF).
Persona prose is kept out of that call so it cannot fight the JSON schema.
Final `{reply}` is what Discord sees. Secrets never enter the planner prompt.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

import db
from cleaner import clean_output
from tools.browser import format_block_message, normalize_persona
from tools.registry import run_tool

MAX_STEPS = 6
WALL_CLOCK_S = 60
TOOL_RESULT_CAP = 1800
MIN_MODEL_SECONDS = 8
PLANNER_TOKENS = 96
PLANNER_TEMP = 0.2

GRAMMAR_DIR = Path(__file__).resolve().parent / "grammar"
GRAMMAR = (GRAMMAR_DIR / "tool_call.gbnf").read_text(encoding="utf-8") if (GRAMMAR_DIR / "tool_call.gbnf").is_file() else ""
GRAMMAR_TOOL_ONLY = (GRAMMAR_DIR / "tool_only.gbnf").read_text(encoding="utf-8") if (GRAMMAR_DIR / "tool_only.gbnf").is_file() else GRAMMAR
GRAMMAR_REPLY_ONLY = (GRAMMAR_DIR / "reply_only.gbnf").read_text(encoding="utf-8") if (GRAMMAR_DIR / "reply_only.gbnf").is_file() else ""
BROWSE_HINT = re.compile(r"\b(open|visit|browse|click|go to|goto|look up|log in|sign in|read)\b", re.I)
RECALL_HINT = re.compile(r"\b(remember|memories|memory|last time|what did you|what do you know)\b", re.I)

URL_RE = re.compile(r"https?://[^\s<>\"']+")

PERSONA_ONE_LINERS = {
    "wendy": "Wendy: warm, short, no speeches.",
    "gumbo": "Gumbo: loud, punchy, no TED talk.",
    "tabatha": "Tabatha: playful, compact.",
    "tech": "Tech: exact, no fluff.",
    "creative": "Creative: sensory, brief.",
    "analysis": "Analysis: structured, brief.",
}


def known_personas() -> tuple[str, ...]:
    path = Path(__file__).resolve().parent / "personas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return tuple(data.keys())
    except Exception:
        return ("wendy", "gumbo", "tabatha")


def resolve_agent_name(persona: Optional[str], bot_name: Optional[str]) -> str:
    names = {n.lower() for n in known_personas()}
    for candidate in (persona, bot_name):
        if candidate and str(candidate).lower() in names:
            return str(candidate).lower()
    return normalize_persona(bot_name or persona or "unknown")


def parse_tool_call(text: str) -> Optional[dict[str, Any]]:
    plan = parse_plan(text)
    if plan and plan.get("kind") == "tool":
        return {"tool": plan["tool"], "args": plan.get("args") or {}}
    return None


def parse_plan(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    candidates = []
    try:
        candidates.append(json.loads(stripped))
    except Exception:
        pass
    start = stripped.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        end = None
        for i, ch in enumerate(stripped[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            break
        try:
            candidates.append(json.loads(stripped[start : end + 1]))
        except Exception:
            pass
        start = stripped.find("{", start + 1)
    for data in candidates:
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("reply"), str) and not data.get("tool"):
            return {"kind": "reply", "reply": data["reply"]}
        if data.get("tool"):
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            return {"kind": "tool", "tool": str(data["tool"]), "args": args}
    return None


def _compact_result(result: Any) -> str:
    if isinstance(result, dict):
        slim = {
            k: result[k]
            for k in ("status", "reason", "url", "title", "text", "error", "clicked", "message", "logged_in")
            if k in result
        }
        actions = result.get("actions")
        if isinstance(actions, dict):
            slim["buttons"] = (actions.get("buttons") or [])[:6]
            slim["links"] = [
                {"text": x.get("text"), "href": x.get("href")}
                for x in (actions.get("links") or [])[:6]
                if isinstance(x, dict)
            ]
            slim["inputs"] = actions.get("inputs") or []
        text = slim.get("text")
        if isinstance(text, str) and len(text) > 700:
            slim["text"] = text[:700]
        result = slim
    blob = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
    return blob[:TOOL_RESULT_CAP]


def _must_act_first(user_prompt: str, working: dict, steps: list) -> bool:
    if steps:
        return False
    # Only force a tool when the user named a URL. Recall questions must be
    # allowed to answer from durable memories without browsing.
    return bool(URL_RE.search(user_prompt or ""))


def _should_reply_now(user_prompt: str, steps: list, working: dict) -> bool:
    """Stop tool-churn: once we have page text that answers, force a reply."""
    if not steps or not (working.get("last_read") or working.get("title")):
        return False
    tools = [s["tool"] for s in steps]
    prompt_l = (user_prompt or "").lower()
    wants_click = "click" in prompt_l
    wants_login = "secret" in prompt_l or "log in" in prompt_l or "sign in" in prompt_l
    if wants_login and "browser_login" not in tools:
        title = (working.get("title") or "").lower()
        url = (working.get("url") or "").lower()
        on_login = "log in" in title or "/login" in url or "sign in" in title
        if on_login:
            return False
        # Already past the wall (e.g. storageState session). Answer from the page.
        return bool(working.get("last_read"))
    if tools[-2:] == ["browser_read", "browser_read"]:
        return True
    if wants_click:
        if "browser_click" not in tools:
            return False
        idx = tools.index("browser_click")
        return "browser_read" in tools[idx + 1 :]
    if wants_login:
        if "browser_login" in tools:
            idx = tools.index("browser_login")
            return "browser_read" in tools[idx + 1 :]
        return False
    return "browser_read" in tools or "web_fetch" in tools


def _call_planner(endpoint: str, prompt: str, n_predict: int, grammar: str | None = None) -> str:
    payload = {
        "prompt": prompt,
        "n_predict": min(n_predict or PLANNER_TOKENS, PLANNER_TOKENS),
        "temperature": PLANNER_TEMP,
        "stop": ["\n\nUser:", "\nUser:"],
    }
    chosen = grammar if grammar is not None else GRAMMAR
    if chosen:
        payload["grammar"] = chosen
    r = requests.post(endpoint, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    raw = data.get("content") if isinstance(data, dict) else ""
    return raw or ""


def _planner_prompt(agent_name: str, user_prompt: str, working: dict, steps: list, sites: list[str]) -> str:
    tone = PERSONA_ONE_LINERS.get(agent_name, f"{agent_name}: brief.")
    recall = bool(RECALL_HINT.search(user_prompt or ""))
    page = "none"
    if working.get("url") and not recall:
        page = f"{working.get('title') or ''} {working.get('url')}"
    last_read = "" if recall else (working.get("last_read") or "")[:500]
    trace = []
    for i, step in enumerate(steps, 1):
        trace.append(f"{i}. {step['tool']} -> {step['result'][:350]}")
    sites_line = ", ".join(sites) if sites else "none"
    notes = db.get_memories(agent_name, limit=5)
    memory_block = "Durable memories:\n" + "\n".join(f"- {n}" for n in notes) if notes else "Durable memories: (none)"
    last_actions = ""
    if steps:
        try:
            last = json.loads(steps[-1]["result"])
            bits = []
            if last.get("buttons"):
                bits.append("buttons: " + ", ".join(f"text={b}" for b in last["buttons"][:6]))
            if last.get("links"):
                bits.append(
                    "links: "
                    + ", ".join(
                        f"text={x.get('text')}" for x in last["links"][:6] if isinstance(x, dict)
                    )
                )
            last_actions = "\n".join(bits)
        except Exception:
            last_actions = ""
    return (
        f"You are a tool planner for {tone}\n"
        "Output ONE JSON object. Either a tool call or a final reply.\n"
        'Tool: {"tool":"browser_goto","args":{"url":"https://example.com"}}\n'
        'Tools: browser_goto, browser_read, browser_click (args.selector like text=Learn more), '
        "browser_type, browser_submit, browser_login, web_fetch.\n"
        'Prefer browser_* when you will click or log in. Use browser_login when the page needs a sign-in '
        "and we have credentials.\n"
        f"Credential sites: {sites_line}\n"
        f"{memory_block}\n"
        "When you have enough from Page/Excerpt/Trace/memories, reply with a factual sentence. "
        "If the user asks what you remember, use Durable memories and reply — do not invent a browse. "
        "If Page is none and they named a URL, call a tool first — do not reply yet.\n"
        f"User: {user_prompt}\n"
        f"Page: {page}\n"
        f"Excerpt: {last_read}\n"
        f"{last_actions}\n"
        f"Trace:\n" + ("\n".join(trace) if trace else "(none)") + "\n"
        "Next:\n"
    )


def _finalize_text(stopped: str, final_raw: str) -> str:
    text = (final_raw or "").strip()
    if stopped in ("blocked", "budget", "model_error"):
        return text
    return clean_output(text) or text


def run_bounded_agent(
    *,
    prompt: str,
    persona: Optional[str],
    task: Optional[str],
    user_id: str,
    bot_name: str,
    endpoint: str,
    n_predict: int,
) -> dict[str, Any]:
    agent_name = resolve_agent_name(persona, bot_name)
    db.ensure_agent(agent_name)
    deadline = time.time() + WALL_CLOCK_S
    steps: list[dict[str, Any]] = []
    working = db.get_session_context(agent_name)
    turns = list(working.get("recent_turns") or [])
    turns.append({"role": "user", "content": prompt[:500]})
    sites = db.list_credential_sites(agent_name)
    tried_login = False
    used_grammar = bool(GRAMMAR)

    def record_step(tool_name, args, result):
        compact = _compact_result(result)
        public_args = {k: v for k, v in (args or {}).items() if k not in ("password", "username")}
        steps.append({"tool": tool_name, "args": public_args, "result": compact})
        db.log_agent_event(
            agent_name,
            tool_name,
            {
                "url": result.get("url") if isinstance(result, dict) else None,
                "title": result.get("title") if isinstance(result, dict) else None,
                "status": result.get("status") if isinstance(result, dict) else "ok",
                "reason": result.get("reason") if isinstance(result, dict) else None,
                "user_id": user_id,
            },
        )
        if isinstance(result, dict):
            old_url = working.get("url")
            if result.get("url"):
                working["url"] = result["url"]
            if result.get("title"):
                working["title"] = result["title"]
            if result.get("text"):
                working["last_read"] = result["text"]
            elif result.get("url") and result.get("url") != old_url:
                working["last_read"] = ""
            working["last_tool"] = {"name": tool_name, "status": result.get("status")}
        return result

    def finish(stopped, final_raw, blocked_reason=None):
        clean = _finalize_text(stopped, final_raw)
        turns.append({"role": "assistant", "content": (clean or final_raw)[:500]})
        working["recent_turns"] = turns
        working["task_notes"] = (prompt or "")[:240]
        db.set_session_context(agent_name, working)
        db.maybe_consolidate(agent_name, user_id=user_id, force=True)
        return {
            "clean": clean,
            "raw": final_raw,
            "steps": steps,
            "stopped": stopped,
            "blocked": stopped == "blocked",
            "blocked_reason": blocked_reason,
            "agent": agent_name,
            "planner": "gbnf" if used_grammar else "fallback",
        }

    final_raw = ""
    stopped = "answered"
    blocked_reason = None

    while len(steps) < MAX_STEPS and time.time() < deadline:
        remaining = deadline - time.time()
        if remaining < MIN_MODEL_SECONDS and steps:
            stopped = "budget"
            break
        try:
            if RECALL_HINT.search(prompt or "") and db.get_memories(agent_name) and GRAMMAR_REPLY_ONLY:
                grammar = GRAMMAR_REPLY_ONLY
            elif _should_reply_now(prompt, steps, working) and GRAMMAR_REPLY_ONLY:
                grammar = GRAMMAR_REPLY_ONLY
            elif _must_act_first(prompt, working, steps):
                grammar = GRAMMAR_TOOL_ONLY
            else:
                grammar = GRAMMAR
            raw = _call_planner(
                endpoint,
                _planner_prompt(agent_name, prompt, working, steps, sites),
                n_predict,
                grammar=grammar,
            )
        except Exception as exc:
            stopped = "model_error"
            final_raw = f"The model dropped out ({exc}). Here's what I had before that."
            if steps:
                final_raw += " " + " | ".join(s["result"][:160] for s in steps[-2:])
            break
        plan = parse_plan(raw)
        if plan is None:
            # Grammar failed or unconstrained junk. Last-resort URL bootstrap once.
            seeded = URL_RE.search(prompt or "")
            if seeded and not steps:
                seed_url = seeded.group(0).rstrip(").,]")
                goto = run_tool(
                    "browser_goto",
                    {"url": seed_url, "persona": agent_name},
                    bot_name=agent_name,
                    user_id=user_id,
                )
                record_step("browser_goto", {"url": seed_url}, goto)
                continue
            final_raw = raw or "I couldn't plan the next step."
            stopped = "answered"
            break
        if plan["kind"] == "reply":
            final_raw = plan["reply"]
            stopped = "answered"
            break

        tool_name = plan["tool"]
        args = dict(plan.get("args") or {})
        args.setdefault("persona", agent_name)
        prompt_urls = [u.rstrip(").,]") for u in URL_RE.findall(prompt or "")]
        if tool_name in ("browser_goto", "web_fetch") and prompt_urls:
            arg_url = args.get("url") or ""
            if not arg_url or not any(p in arg_url or arg_url in p for p in prompt_urls):
                args["url"] = prompt_urls[0]
        result = run_tool(tool_name, args, bot_name=agent_name, user_id=user_id)
        record_step(tool_name, args, result)

        if isinstance(result, dict) and result.get("status") == "blocked":
            if result.get("reason") == "login_wall" and not tried_login:
                tried_login = True
                login = run_tool(
                    "browser_login",
                    {"url": result.get("url") or args.get("url"), "persona": agent_name},
                    bot_name=agent_name,
                    user_id=user_id,
                )
                record_step("browser_login", {"url": result.get("url")}, login)
                if isinstance(login, dict) and login.get("status") != "blocked":
                    read = run_tool(
                        "browser_read",
                        {"persona": agent_name},
                        bot_name=agent_name,
                        user_id=user_id,
                    )
                    record_step("browser_read", {}, read)
                    continue
                result = login
            stopped = "blocked"
            blocked_reason = result.get("reason") if isinstance(result, dict) else "challenge"
            final_raw = (
                (result.get("message") if isinstance(result, dict) else None)
                or format_block_message(result if isinstance(result, dict) else {})
            )
            break
    else:
        if not final_raw:
            stopped = "budget"

    if stopped == "budget" and not final_raw:
        bits = [s["result"][:200] for s in steps] or ["nothing yet"]
        final_raw = "I ran out of time. Here's what I found so far: " + " | ".join(bits)

    return finish(stopped, final_raw, blocked_reason)
