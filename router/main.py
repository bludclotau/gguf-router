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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_schema()
    yield


app = FastAPI(title="gguf-router", lifespan=lifespan)


class RouteRequest(BaseModel):
    prompt: str
    persona: Optional[str] = None
    task: Optional[str] = None
    n_predict: Optional[int] = Field(default=256)
    max_tokens: Optional[int] = None
    stream: bool = False


def select_model(persona: Optional[str], task: Optional[str]) -> str:
    if persona:
        mapped = PERSONAS.get(persona) or PERSONAS.get(persona.lower())
        if mapped:
            return mapped
    if task:
        mapped = TASKS.get(task) or TASKS.get(task.lower())
        if mapped:
            return mapped
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
        "postgres": bool(db.DSN),
    }


@app.post("/route")
def route(body: RouteRequest) -> Any:
    model_name = select_model(body.persona, body.task)
    endpoint = MODELS.get(model_name)
    if not endpoint:
        raise HTTPException(status_code=500, detail=f"No endpoint configured for model '{model_name}'")

    n_predict = body.n_predict if body.n_predict is not None else body.max_tokens
    if n_predict is None:
        n_predict = 256

    payload = {
        "prompt": body.prompt,
        "n_predict": n_predict,
        "stream": False,
    }

    try:
        response = requests.post(endpoint, json=payload, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"{model_name} request failed: {exc}") from exc

    try:
        upstream = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"{model_name} returned non-JSON") from exc

    raw_output = extract_raw_text(upstream)
    conversation_id = db.save_raw_output(
        prompt=body.prompt,
        raw_output=raw_output,
        persona=body.persona,
        task=body.task,
        model=model_name,
    )
    cleaned = clean_output(raw_output)
    db.save_clean_output(conversation_id, cleaned)

    return {
        "content": cleaned,
        "reply": cleaned,
        "model": model_name,
        "persona": body.persona,
        "task": body.task,
        "conversation_id": conversation_id,
    }
