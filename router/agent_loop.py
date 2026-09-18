"""Bounded propose → tool → re-prompt loop.

Caps: MAX_STEPS tool calls and WALL_CLOCK_S seconds. This is meant to feel
like a chat reply, not a background crawler. If the budget expires we
return a partial "here's what I found" instead of hanging Discord.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import requests

import db
from cleaner import clean_output
from persona_engine import apply
from tools.registry import run_tool

MAX_STEPS = 5
WALL_CLOCK_S = 45
TOOL_RESULT_CAP = 1800

TOOL_HINT = """
You may use tools. Output ONLY a JSON object to call one:
{"tool":"browser_goto","args":{"url":"https://example.com"}}
{"tool":"browser_read","args":{}}
{"tool":"browser_click","args":{"selector":"text=Learn more"}}
{"tool":"browser_type","args":{"selector":"#q","text":"hello"}}
{"tool":"browser_submit","args":{"selector":"form"}}
{"tool":"web_fetch","args":{"url":"https://example.com"}}
When you can answer the user, reply in character with no JSON and no tool call.
""".strip()

JSON_TOOL = re.compile(
    r"\{[^{}]*\"tool\"\s*:\s*\"([a-zA-Z0-9_]+)\"[^{}]*\}",
    re.DOTALL,
)
URL_RE = re.compile(r"https?://[^\s<>\"']+")


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
    match = JSON_TOOL.search(stripped)
    if not match:
        return None
    blob = match.group(0)
    try:
        data = json.loads(blob)
    except Exception:
        # args may contain nested braces; scan from first {
        start = stripped.find("{")
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
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
                    try:
                        data = json.loads(stripped[start : i + 1])
                        break
                    except Exception:
                        return None
        else:
            return None
    if isinstance(data, dict) and data.get("tool"):
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        return {"tool": str(data["tool"]), "args": args}
    return None


def _compact_result(result: Any) -> str:
    if isinstance(result, dict):
        slim = {
            k: result[k]
            for k in ("status", "reason", "url", "title", "text", "error", "clicked")
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
    agent_name = (bot_name or persona or "wendy").lower()
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

    # Local models rarely emit valid tool JSON. If the user pasted a URL,
    # go there and read first so !browse has real page text in context.
    seeded = URL_RE.search(prompt or "")
    if seeded:
        seed_url = seeded.group(0).rstrip(").,]")
        goto = run_tool("browser_goto", {"url": seed_url, "persona": agent_name}, bot_name=agent_name, user_id=user_id)
        record_step("browser_goto", {"url": seed_url}, goto)
        if isinstance(goto, dict) and goto.get("status") != "blocked":
            read = run_tool("browser_read", {"persona": agent_name}, bot_name=agent_name, user_id=user_id)
            record_step("browser_read", {}, read)

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
            persona,
            task,
            prompt,
            user_id,
            tool="browser",
            tool_result=extra or None,
        )
        return assembled

    final_raw = ""
    stopped = "answered"
    while len(steps) < MAX_STEPS and time.time() < deadline:
        remaining = deadline - time.time()
        if remaining < 4:
            stopped = "budget"
            break
        try:
            raw = _call_model(endpoint, build_prompt(steps), n_predict)
        except Exception as exc:
            stopped = "model_error"
            final_raw = f"Model error: {exc}"
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
            final_raw = (
                f"I got blocked ({result.get('reason') or 'challenge'}) on "
                f"{result.get('url') or 'that page'}. Here's what I had: "
                f"{result.get('title') or ''} {result.get('text') or compact}"
            )
            break
    else:
        if not final_raw:
            stopped = "budget"

    if stopped == "budget" and not final_raw:
        bits = [s["result"][:200] for s in steps] or ["nothing yet"]
        final_raw = "Here's what I found so far: " + " | ".join(bits)

    clean = clean_output(final_raw)
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
        "agent": agent_name,
    }
