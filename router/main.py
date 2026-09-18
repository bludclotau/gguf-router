from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import json
import yaml
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import db
from cleaner import clean_output
from persona_engine import apply

BASE_DIR = Path(__file__).resolve().parent

with open(BASE_DIR / "config.yaml", encoding="utf-8") as fh:
    CONFIG = yaml.safe_load(fh)

with open(BASE_DIR / "personas.json", encoding="utf-8") as fh:
    PERSONAS = json.load(fh)

with open(BASE_DIR / "tasks.json", encoding="utf-8") as fh:
    TASKS = json.load(fh)

MODELS = CONFIG["models"]
DEFAULT_MODEL = "qwen"
REQUEST_TIMEOUT_S = 240
COOLDOWN_SECONDS = 2


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_schema()
    yield


app = FastAPI(title="gguf-router", lifespan=lifespan)


class RouteRequest(BaseModel):
    prompt: str
    persona: Optional[str] = None
    task: Optional[str] = None
    user_id: Optional[str] = "unknown"
    bot_name: Optional[str] = "discord"
    n_predict: Optional[int] = Field(default=256)
    max_tokens: Optional[int] = None
    stream: bool = False


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
    return {
        "status": "ok",
        "models": list(MODELS.keys()),
        "personas": PERSONAS,
        "tasks": TASKS,
        "postgres": True,
    }


@app.post("/route")
def route(payload: RouteRequest) -> Any:
    prompt = payload.prompt
    persona = payload.persona
    task = payload.task
    user_id = payload.user_id or "unknown"
    bot_name = payload.bot_name or "discord"

    if db.check_cooldown(user_id):
        return {"clean": "Cooldown active.", "raw": None}

    final_prompt, model = apply(persona, task, prompt, user_id)
    endpoint = MODELS.get(model, MODELS["qwen"])

    n_predict = payload.n_predict if payload.n_predict is not None else payload.max_tokens
    if n_predict is None:
        n_predict = 256

    try:
        r = requests.post(
            endpoint,
            json={"prompt": final_prompt, "n_predict": n_predict},
            timeout=REQUEST_TIMEOUT_S,
        )
        r.raise_for_status()
        raw = r.json().get("content", "")
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"{model} request failed: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"{model} returned non-JSON") from exc

    db.save_raw_output(f"raw:{user_id}", raw)
    clean = clean_output(raw)
    db.update_conversation(user_id, bot_name, persona, task, clean)
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
