import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import chat_dispatch
import json
import yaml
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

import db
from agent_loop import run_bounded_agent
from cleaner import clean_output
from persona_engine import apply
from tools import browser as browser_tools
from tools.registry import run_tool

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.yaml", encoding="utf-8") as fh:
    CONFIG = yaml.safe_load(fh)

with open(BASE_DIR / "personas.json", encoding="utf-8") as fh:
    PERSONAS = json.load(fh)

with open(BASE_DIR / "tasks.json", encoding="utf-8") as fh:
    TASKS = json.load(fh)

MODELS = CONFIG["models"]
DEFAULT_MODEL = "qwen"
REQUEST_TIMEOUT_S = 120
COOLDOWN_SECONDS = 2


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_schema()
    yield
    browser_tools.shutdown()


app = FastAPI(title="gguf-router", lifespan=lifespan)


class RouteRequest(BaseModel):
    prompt: str
    persona: Optional[str] = None
    task: Optional[str] = None
    user_id: Optional[str] = "unknown"
    bot_name: Optional[str] = "discord"
    tool: Optional[str] = None
    args: Optional[dict] = None
    n_predict: Optional[int] = Field(default=256)
    max_tokens: Optional[int] = None
    stream: bool = False
    agent: bool = False


def select_model(persona: Optional[str], task: Optional[str]) -> str:
    if persona:
        mapped = PERSONAS.get(persona) or PERSONAS.get(persona.lower())
        if mapped:
            return mapped
        if persona in MODELS or (persona and persona.lower() in MODELS):
            return persona if persona in MODELS else persona.lower()
    if task:
        mapped = TASKS.get(task) or TASKS.get(task.lower())
        if mapped:
            return mapped
        if task in MODELS or (task and task.lower() in MODELS):
            return task if task in MODELS else task.lower()
    return DEFAULT_MODEL


def extract_raw_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return str(payload)

    for key in ("content", "reply", "text", "response"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            if isinstance(first.get("text"), str) and first["text"]:
                return first["text"]
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
    return json.dumps(payload)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "router-ok"}


@app.get("/debug/login", response_class=HTMLResponse)
def debug_login():
    """Local login fixture for auto-login tests. Not a public product surface."""
    return """<!doctype html><html><head><title>Log in</title></head><body>
    <h1>Log in</h1>
    <form method="get" action="/debug/login/go">
      <input name="username" type="text">
      <input name="password" type="password">
      <button type="submit">Log in</button>
    </form>
    </body></html>"""


@app.get("/debug/login/go")
def debug_login_go(username: str = "", password: str = ""):
    if username == "wendy" and password == "snacktime":
        resp = RedirectResponse("/debug/secret", status_code=303)
        resp.set_cookie("fixture_auth", "wendy", httponly=True)
        return resp
    return HTMLResponse("<html><title>Log in</title><p>bad credentials</p></html>", status_code=401)


@app.get("/debug/secret", response_class=HTMLResponse)
def debug_secret(request: Request):
    if request.cookies.get("fixture_auth") != "wendy":
        return RedirectResponse("/debug/login", status_code=303)
    return "<html><title>Secret</title><h1>SECRET waffle-iron-42</h1></html>"


@app.post("/tool")
def tool(payload: dict) -> Any:
    name = payload.get("tool")
    args = payload.get("args", {}) or {}
    user_id = payload.get("user_id", "unknown")
    bot_name = payload.get("bot_name", "discord")

    agent_name = payload.get("persona") or bot_name
    result = run_tool(name, args, bot_name=agent_name, user_id=user_id)
    db.async_write(db.save_raw_output, f"tool:{user_id}:{name}", str(result))
    db.log_tool(user_id, name, str(result))
    db.log_agent_event(
        agent_name,
        name or "tool",
        {"user_id": user_id, "status": (result or {}).get("status") if isinstance(result, dict) else "ok"},
    )
    db.touch_session_from_tool(agent_name, result)
    if isinstance(result, dict) and result.get("status") == "blocked":
        db.maybe_consolidate(agent_name, user_id=user_id, force=True)
        result = dict(result)
        result.setdefault("message", result.get("message"))
    else:
        db.maybe_consolidate(agent_name, user_id=user_id, force=False)
    return {"result": result, "user_id": user_id, "bot_name": bot_name, "agent": agent_name}


@app.post("/route")
def route(payload: RouteRequest) -> Any:
    prompt = payload.prompt
    persona = payload.persona
    task = payload.task
    user_id = payload.user_id or "unknown"
    bot_name = payload.bot_name or "discord"

    if db.check_cooldown(user_id):
        return {"clean": "Cooldown active.", "raw": None}

    triggered = chat_dispatch.first_http_url(prompt) if not payload.tool else None
    if triggered:
        return _dispatch_wendy(payload, user_id, bot_name, persona, triggered)

    tool_name = payload.tool
    tool_result = None
    if tool_name:
        tool_result = run_tool(tool_name, payload.args or {}, bot_name=bot_name, user_id=user_id)
        db.async_write(db.save_raw_output, f"tool:{user_id}:{tool_name}", str(tool_result))
        db.log_tool(user_id, tool_name, str(tool_result))
        db.log_agent_event(
            persona or bot_name,
            tool_name,
            {"user_id": user_id, "status": (tool_result or {}).get("status") if isinstance(tool_result, dict) else "ok"},
        )

    final_prompt, model = apply(
        persona,
        task,
        prompt,
        user_id,
        tool=tool_name or ("browser" if payload.agent else None),
        tool_result=tool_result,
    )
    endpoint = MODELS.get(model, MODELS["qwen"])

    if model == "qwen":
        try:
            requests.get("http://10.1.1.122:8081/health", timeout=2)
        except Exception:
            model = "dolphin"
            endpoint = MODELS["dolphin"]

    n_predict = payload.n_predict if payload.n_predict is not None else payload.max_tokens
    if n_predict is None:
        n_predict = 256

    if payload.agent:
        outcome = run_bounded_agent(
            prompt=prompt,
            persona=persona,
            task=task,
            user_id=user_id,
            bot_name=bot_name,
            endpoint=endpoint,
            n_predict=n_predict,
        )
        clean = outcome["clean"]
        raw = outcome["raw"]
        db.async_write(db.save_raw_output, f"raw:{user_id}", raw)
        db.async_write(db.update_conversation, user_id, bot_name, persona, task, clean)
        db.set_cooldown(user_id, COOLDOWN_SECONDS)
        return {
            "clean": clean,
            "raw": raw,
            "content": clean,
            "reply": clean,
            "model": model,
            "persona": persona,
            "task": task,
            "user_id": user_id,
            "bot_name": bot_name,
            "agent": outcome.get("agent"),
            "stopped": outcome.get("stopped"),
            "blocked": bool(outcome.get("blocked")),
            "blocked_reason": outcome.get("blocked_reason"),
            "planner": outcome.get("planner"),
            "steps": outcome.get("steps"),
        }

    try:
        r = requests.post(
            endpoint,
            json={"prompt": final_prompt, "n_predict": n_predict},
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json().get("content", "")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"{model} request failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"{model} returned non-JSON") from exc

    db.async_write(db.save_raw_output, f"raw:{user_id}", raw)
    clean = clean_output(raw)
    db.async_write(db.update_conversation, user_id, bot_name, persona, task, clean)
    db.set_cooldown(user_id, COOLDOWN_SECONDS)

    return {
        "clean": clean,
        "raw": raw,
        "content": clean,
        "reply": clean,
        "model": model,
        "persona": persona,
        "task": task,
        "user_id": user_id,
        "bot_name": bot_name,
    }


def _dispatch_wendy(payload: RouteRequest, user_id: str, bot_name: str, persona: Optional[str], url: str) -> dict:
    job_id = chat_dispatch.start_job(user_id, url, payload.prompt)
    db.async_write(db.update_conversation, user_id, bot_name, persona, "wendy", chat_dispatch.ACK)
    db.set_cooldown(user_id, COOLDOWN_SECONDS)

    def worker() -> None:
        text = ""
        try:
            outcome = requests.post(
                os.environ.get("WENDY_PIPELINE_URL", "http://127.0.0.1:8790/pipeline"),
                json={"prompt": payload.prompt, "url": url, "user_id": user_id, "persona": persona or "wendy"},
                headers={"Authorization": f"Bearer {os.environ.get('TOOL_API_TOKEN', '')}"},
                timeout=180,
            )
            outcome.raise_for_status()
            body = outcome.json()
            text = chat_dispatch.followup_text(url, body)
            chat_dispatch.finish_job(job_id, text, body)
        except Exception as exc:
            text = f"I couldn't finish looking at that. {exc}"
            chat_dispatch.fail_job(job_id, str(exc))
        db.async_write(db.update_conversation, user_id, bot_name, persona, "wendy-followup", text)

    chat_dispatch.launch(job_id, worker)
    return {
        "clean": chat_dispatch.ACK,
        "raw": chat_dispatch.ACK,
        "content": chat_dispatch.ACK,
        "reply": chat_dispatch.ACK,
        "dispatched": True,
        "phase": "ack",
        "job_id": job_id,
        "url": url,
        "persona": persona,
        "user_id": user_id,
        "bot_name": bot_name,
    }


@app.get("/route/jobs/{job_id}")
def route_job(job_id: str) -> Any:
    job = chat_dispatch.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    return job
