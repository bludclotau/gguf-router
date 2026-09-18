import json
from pathlib import Path

import yaml

import db

BASE_DIR = Path(__file__).resolve().parent

# Load persona → model mapping
with open(BASE_DIR / "personas.json", encoding="utf-8") as fh:
    personas = json.load(fh)

# Load task → model mapping
with open(BASE_DIR / "tasks.json", encoding="utf-8") as fh:
    tasks = json.load(fh)

# Load persona pre-prompts
with open(BASE_DIR / "persona_prompts.yaml", encoding="utf-8") as fh:
    persona_prompts = yaml.safe_load(fh) or {}


def _lookup(mapping, key):
    if not key:
        return None
    return mapping.get(key) or mapping.get(str(key).lower())


def apply(persona, task, user_prompt, user_id, tool=None, tool_result=None):
    # Determine model
    mapped_persona = _lookup(personas, persona)
    mapped_task = _lookup(tasks, task)
    if mapped_persona:
        model = mapped_persona
    elif mapped_task:
        model = mapped_task
    else:
        model = "qwen"

    # Base persona pre-prompt
    pre = _lookup(persona_prompts, persona) or ""

    if tool:
        pre += "\nAssistant, you have access to tools. Use them when needed.\n"

    # Persona memory
    memory = db.get_persona_memory(user_id, persona)
    if memory:
        pre += f"\nYour previous interaction memory: {memory}\n"

    # Task-specific modifier
    if task:
        pre += f"\nTask context: {task}\n"

    body = user_prompt or ""
    already_formatted = (
        "[Assistant]" in body
        or "[User]" in body
        or "[System]" in body
        or body.lstrip().startswith("User:")
        or "Assistant:" in body
    )

    result_text = ""
    if tool_result is not None:
        result_text = str(tool_result)
        if len(result_text) > 4000:
            result_text = result_text[:4000]

    if tool_result is not None and not already_formatted:
        final_prompt = (
            f"{pre}\n"
            f"Tool result: {result_text}\n"
            f"User: {body}\n"
            f"Assistant:"
        )
    elif already_formatted:
        if result_text:
            pre = f"{pre}\nTool result: {result_text}\n"
        final_prompt = f"{pre}\n{body}".strip() if pre else body.strip()
        if not final_prompt.endswith("Assistant:") and not final_prompt.rstrip().endswith("[Assistant]"):
            final_prompt += "\nAssistant:"
    else:
        final_prompt = (
            f"{pre}\n"
            f"User: {body}\n"
            f"Assistant:"
        )

    return final_prompt, model
