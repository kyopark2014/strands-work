"""Execute a scheduled job via the same chat path as a user message."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Optional

from application import schedule_store
from application import task_store
from application.services.chat_stream_service import ChatStreamService
from application.task_store_persistence import flush_persist

logger = logging.getLogger("scheduled_job_service")

_USER_PREFIX = "[예약 실행] "


def run_scheduled_job(job_id: str) -> dict[str, Any]:
    """Load job from DynamoDB and run prompt against the bound task.

    Returns immediately after starting the worker when ``async_mode`` is used
    by the HTTP layer; this function itself runs synchronously to completion
    when called from a background thread.
    """
    job = schedule_store.get_job(job_id)
    if not job:
        raise LookupError(f"job not found: {job_id}")
    if not job.get("enabled", True):
        schedule_store.mark_run(job_id, status="skipped", error="job disabled")
        return {"ok": True, "status": "skipped", "reason": "disabled"}

    user_id = (job.get("user_id") or "").strip()
    task_id = (job.get("task_id") or "").strip()
    prompt = (job.get("prompt") or "").strip()
    if not user_id or not task_id or not prompt:
        schedule_store.mark_run(job_id, status="error", error="incomplete job")
        raise ValueError("job missing user_id, task_id, or prompt")

    task = task_store.get_task_refreshing(task_id, user_id)
    if not task:
        schedule_store.mark_run(job_id, status="error", error="task not found")
        raise LookupError(f"task not found: {task_id}")

    # Prefer live task session; fall back to job snapshot.
    runtime_session_id = (
        (task.get("runtime_session_id") or "").strip()
        or (job.get("runtime_session_id") or "").strip()
        or task_id
    )

    display_prompt = prompt if prompt.startswith(_USER_PREFIX) else f"{_USER_PREFIX}{prompt}"

    service = ChatStreamService()
    message_queue, result_holder = service.start_chat_stream(
        task_id=task_id,
        task={**task, "runtime_session_id": runtime_session_id},
        user_id=user_id,
        prompt=display_prompt,
        files=[],
    )

    # Drain SSE-style queue until worker finishes (no HTTP client).
    tool_events: list[dict[str, Any]] = []
    while True:
        try:
            item = message_queue.get(timeout=600)
        except queue.Empty:
            schedule_store.mark_run(job_id, status="error", error="timeout")
            raise TimeoutError("scheduled job timed out")
        if item is None:
            break
        # Discard stream events; final content is in result_holder.

    error = result_holder.get("error")
    if error:
        task_store.add_message(
            task_id,
            "assistant",
            f"Error: {error}",
            user_id=user_id,
        )
        schedule_store.mark_run(job_id, status="error", error=str(error)[:500])
        flush_persist(user_id)
        return {"ok": False, "status": "error", "error": error}

    content = result_holder.get("content") or ""
    images = result_holder.get("images") or []
    task_store.add_message(
        task_id,
        "assistant",
        content,
        user_id=user_id,
        images=images,
        tool_events=tool_events,
    )
    flush_persist(user_id)
    schedule_store.mark_run(job_id, status="ok")
    logger.info("Scheduled job %s completed for task %s", job_id, task_id)
    return {"ok": True, "status": "ok", "job_id": job_id, "task_id": task_id}


def start_scheduled_job_async(job_id: str) -> None:
    def _run() -> None:
        try:
            run_scheduled_job(job_id)
        except Exception:
            logger.exception("Async scheduled job failed: %s", job_id)

    threading.Thread(target=_run, daemon=True, name=f"schedule-{job_id[:8]}").start()
