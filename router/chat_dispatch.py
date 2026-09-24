"""URL messages on /route ack immediately, then Wendy runs the page pipeline."""

import re
import threading
import uuid
from typing import Any, Callable, Optional

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
ACK = "let me take a look, one sec"

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def first_http_url(text: str) -> Optional[str]:
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,);]")


def followup_text(url: str, outcome: dict) -> str:
    found = str(outcome.get("found") or outcome.get("error") or "").strip()
    if len(found) > 700:
        found = found[:700].rstrip() + "…"
    pending = outcome.get("pending_id")
    link = outcome.get("proposed_url") or ""
    lines = [f"I looked at {url}."]
    if found:
        lines.append(found)
    if pending and link:
        lines.append(
            f"Publish is waiting for approval (#{pending}). After you approve it, the page is at {link}."
        )
    elif link:
        lines.append(f"The page is at {link}.")
    elif not found:
        lines.append("I couldn't read that page.")
    return "\n".join(lines)


def start_job(user_id: str, url: str, prompt: str) -> str:
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "user_id": user_id,
            "url": url,
            "prompt": prompt,
            "status": "running",
            "ack": ACK,
            "followup": None,
        }
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def finish_job(job_id: str, followup: str, outcome: Optional[dict] = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["status"] = "done"
        job["followup"] = followup
        job["outcome"] = outcome or {}


def fail_job(job_id: str, message: str) -> None:
    finish_job(job_id, f"I couldn't finish looking at that. {message}")
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "error"


def launch(job_id: str, worker: Callable[[], Any]) -> None:
    threading.Thread(target=worker, name=f"wendy-{job_id[:8]}", daemon=True).start()
