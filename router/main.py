from pathlib import Path
from typing import Any, Optional

import json
import yaml
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

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

app = FastAPI(title="gguf-router")


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


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "models": list(MODELS.keys()),
        "personas": PERSONAS,
        "tasks": TASKS,
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
        return response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"{model_name} returned non-JSON") from exc
