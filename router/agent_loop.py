"""Bounded propose → tool → re-prompt loop.

Caps: MAX_STEPS tool calls and WALL_CLOCK_S seconds. This is meant to feel
like a chat reply, not a background crawler. If the budget expires we
return a partial "here's what I found" instead of hanging Discord.
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
from persona_engine import apply
from tools.browser import format_block_message, normalize_persona
from tools.registry import run_tool

MAX_STEPS = 6
WALL_CLOCK_S = 60
TOOL_RESULT_CAP = 1800
MIN_MODEL_SECONDS = 8

TOOL_HINT = """
You may use tools. Output ONLY a JSON object to call one:
{"tool":"browser_goto","args":{"url":"https://example.com"}}
{"tool":"browser_read","args":{}}
{"tool":"browser_click","args":{"selector":"text=Learn more"}}
{"tool":"browser_type","args":{"selector":"#q","text":"hello"}}
{"tool":"browser_submit","args":{"selector":"form"}}
{"tool":"web_fetch","args":{"url":"https://example.com"}}
When you can answer the user, reply in character with no JSON and no tool call.
If a tool result has status "blocked", tell the user you hit a wall. Do not invent page content.
""".strip()

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def known_personas() -> tuple[str, ...]:
    path = Path(__file__).resolve().parent / "personas.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return tuple(data.keys())
    except Exception:
        return ("wendy", "gumbo", "tabatha")


def resolve_agent_name(persona: Optional[str], bot_name: Optional[str]) -> str:
    """Prefer an explicit persona from personas.json, else the Discord bot name."""
    names = {n.lower() for n in known_personas()}
    for candidate in (persona, bot_name):
        if candidate and str(candidate).lower() in names:
            return str(candidate).lower()
    return normalize_persona(bot_name or persona or "unknown")


def parse_tool_call(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    try:
        data = json.loads(stripped)
        if isinstance(data, dict) and data.get("tool"):
            return {"tool": data["tool"], "args": data.get("args") or {}}
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
            data = json.loads(stripped[start : end + 1])
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("tool"):
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            return {"tool": str(data["tool"]), "args": args}
        start = stripped.find("{", start + 1)
    return None


def _compact_result(result: Any) -> str:
    if isinstance(result, dict):
        slim = {
            k: result[k]
            for k in ("status", "reason", "url", "title", "text", "error", "clicked", "message")
            if k in result
        }
        text = slim.get("text")
        if isinstance(text, str) and len(text) > 900:
            slim["text"] = text[:900]
        result = slim
    blob = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
    return blob[:TOOL_RESULT_CAP]


def _call_model(endpoint: str, prompt: str, n_predict: int) -> str:
    r = requests.post(
        endpoint,
        json={"prompt": prompt, "n_predict": n_predict},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    raw = data.get("content") if isinstance(data, dict) else ""
    return raw or ""


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

    memories = db.get_memories(agent_name, limit=4)
    memory_block = ""
    if memories:
        memory_block = "Durable notes:\n" + "\n".join(f"- {m}" for m in memories)

    def record_step(tool_name, args, result):
        compact = _compact_result(result)
        steps.append({"tool": tool_name, "args": args, "result": compact})
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
            if result.get("url"):
                working["url"] = result["url"]
            if result.get("title"):
                working["title"] = result["title"]
            if result.get("text"):
                working["last_read"] = result["text"]
            working["last_tool"] = {"name": tool_name, "status": result.get("status")}
        return result

    seeded = URL_RE.search(prompt or "")
    if seeded:
        seed_url = seeded.group(0).rstrip(").,]")
        goto = run_tool(
            "browser_goto",
            {"url": seed_url, "persona": agent_name},
            bot_name=agent_name,
            user_id=user_id,
        )
        record_step("browser_goto", {"url": seed_url}, goto)
        if isinstance(goto, dict) and goto.get("status") == "blocked":
            msg = goto.get("message") or format_block_message(goto)
            working["recent_turns"] = turns
            db.set_session_context(agent_name, working)
            db.maybe_consolidate(agent_name, user_id=user_id, force=True)
            return {
                "clean": msg,
                "raw": msg,
                "steps": steps,
                "stopped": "blocked",
                "blocked": True,
                "blocked_reason": goto.get("reason"),
                "agent": agent_name,
            }
        read = run_tool(
            "browser_read",
            {"persona": agent_name},
            bot_name=agent_name,
            user_id=user_id,
        )
        record_step("browser_read", {}, read)
        if isinstance(read, dict) and read.get("status") == "blocked":
            msg = read.get("message") or format_block_message(read)
            working["recent_turns"] = turns
            db.set_session_context(agent_name, working)
            db.maybe_consolidate(agent_name, user_id=user_id, force=True)
            return {
                "clean": msg,
                "raw": msg,
                "steps": steps,
                "stopped": "blocked",
                "blocked": True,
                "blocked_reason": read.get("reason"),
                "agent": agent_name,
            }

    def build_prompt(tool_trace: list[dict[str, Any]]) -> str:
        page_line = ""
        if working.get("url"):
            page_line = f"Current page: {working.get('title') or ''} {working.get('url')}\n"
        trace_lines = []
        for i, step in enumerate(tool_trace, 1):
            trace_lines.append(f"{i}. {step['tool']} -> {step['result'][:400]}")
        trace_block = "\n".join(trace_lines)
        extra = "\n".join(
            x for x in (TOOL_HINT, memory_block, page_line, f"Tool trace:\n{trace_block}" if trace_block else "") if x
        )
        assembled, _model = apply(
            persona or agent_name,
            task,
            prompt,
            user_id,
            tool="browser",
            tool_result=extra or None,
        )
        return assembled

    final_raw = ""
    stopped = "answered"
    blocked_reason = None
    while len(steps) < MAX_STEPS and time.time() < deadline:
        remaining = deadline - time.time()
        if remaining < MIN_MODEL_SECONDS and steps:
            stopped = "budget"
            break
        try:
            raw = _call_model(endpoint, build_prompt(steps), n_predict)
        except Exception as exc:
            stopped = "model_error"
            final_raw = f"The model dropped out ({exc}). Here's what I had before that."
            if steps:
                final_raw += " " + " | ".join(s["result"][:160] for s in steps[-2:])
            break
        call = parse_tool_call(raw)
        if not call:
            final_raw = raw
            stopped = "answered"
            break
        tool_name = call["tool"]
        args = dict(call.get("args") or {})
        args.setdefault("persona", agent_name)
        result = run_tool(tool_name, args, bot_name=agent_name, user_id=user_id)
        record_step(tool_name, args, result)
        if isinstance(result, dict) and result.get("status") == "blocked":
            stopped = "blocked"
            blocked_reason = result.get("reason")
            final_raw = result.get("message") or format_block_message(result)
            break
    else:
        if not final_raw:
            stopped = "budget"

    if stopped == "budget" and not final_raw:
        bits = [s["result"][:200] for s in steps] or ["nothing yet"]
        final_raw = "I ran out of time. Here's what I found so far: " + " | ".join(bits)

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
    }
