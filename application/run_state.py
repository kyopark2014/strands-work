# Copyright 2026 Amazon.com, Inc. or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Query chat run status for browser-refresh recovery (MVP).

Resolution order (Strands — no LangGraph checkpoint hydrate):
1. Messages DB (assistant already persisted)
2. In-process run registry (same process, refresh mid-run)

When registry has a finished answer but messages still end on user, hydrate
the assistant row into the messages DB.
"""

from __future__ import annotations

import logging
from typing import Any

from application import run_registry
from application import task_store
from application.tool_events_slim import slim_tool_events

logger = logging.getLogger("run_state")


def _is_streaming_assistant(message: dict[str, Any] | None) -> bool:
    return bool(
        message
        and message.get("role") == "assistant"
        and message.get("status") == task_store.MESSAGE_STATUS_STREAMING
    )


def _hydrate_assistant_if_needed(
    *,
    task_id: str,
    user_id: str,
    content: str,
    images: list[str] | None = None,
) -> bool:
    """Persist assistant message when messages DB still ends on user."""
    content = (content or "").strip()
    if not content:
        return False
    messages = task_store.list_messages(task_id, user_id)
    if (
        messages
        and messages[-1].get("role") == "assistant"
        and not _is_streaming_assistant(messages[-1])
    ):
        return False
    task_store.add_message(
        task_id,
        "assistant",
        content,
        user_id=user_id,
        images=images or [],
        tool_events=[],
    )
    logger.info(
        "Hydrated assistant message from run query (%s chars) task=%s",
        len(content),
        task_id,
    )
    return True


def query_task_run(task_id: str, user_id: str) -> dict[str, Any]:
    """Return run status for a task (query API surface for UI / future runtime)."""
    task = task_store.get_task_refreshing(task_id, user_id)
    if not task:
        return {
            "task_id": task_id,
            "status": "missing",
            "content": "",
            "images": [],
            "error": "Task not found",
            "source": None,
            "hydrated": False,
        }

    messages = task_store.list_messages(task_id, user_id)
    last = messages[-1] if messages else None
    last_role = last["role"] if last else None
    last_streaming = _is_streaming_assistant(last)

    # 1) A finished assistant row means the turn is idle.
    # A streaming draft stays visible while the registry is checked.
    if last_role == "assistant" and not last_streaming:
        last = messages[-1]
        return {
            "task_id": task_id,
            "status": "idle",
            "content": last.get("content") or "",
            "images": last.get("images") or [],
            "error": None,
            "source": "messages",
            "hydrated": False,
        }

    # 2) In-process registry (browser refresh while same process still runs)
    reg = run_registry.get(task_id)
    if reg and reg.get("status") == "running":
        draft_content = (last.get("content") or "") if last_streaming and last else ""
        draft_images = list(last.get("images") or []) if last_streaming and last else []
        live_events = list(reg.get("tool_events") or [])
        if not live_events and last_streaming and last:
            live_events = list(last.get("tool_events") or [])
        return {
            "task_id": task_id,
            "status": "running",
            "content": draft_content,
            "images": draft_images,
            "error": None,
            "source": "registry" if not last_streaming else "messages",
            "hydrated": False,
            "tool_events": slim_tool_events(live_events),
        }

    if reg and reg.get("status") in ("done", "error"):
        if reg.get("cancelled"):
            return {
                "task_id": task_id,
                "status": "done",
                "content": "",
                "images": [],
                "error": None,
                "source": "registry",
                "hydrated": False,
                "cancelled": True,
            }
        content = (reg.get("content") or "").strip()
        error = reg.get("error")
        images = list(reg.get("images") or [])
        if error and not content:
            content = f"Error: {error}"
        # Never hydrate disconnect/cancel noise into the transcript.
        from application import run_cancel

        if run_cancel.is_cancel_noise(content):
            content = ""
        hydrated = False
        if content and (last_role == "user" or last_streaming):
            hydrated = _hydrate_assistant_if_needed(
                task_id=task_id,
                user_id=user_id,
                content=content,
                images=images,
            )
        return {
            "task_id": task_id,
            "status": "error"
            if error and not (reg.get("content") or "").strip()
            else "done",
            "content": content,
            "images": images,
            "error": error,
            "source": "registry",
            "hydrated": hydrated,
        }

    # Empty conversation
    if not messages:
        return {
            "task_id": task_id,
            "status": "idle",
            "content": "",
            "images": [],
            "error": None,
            "source": "messages",
            "hydrated": False,
        }

    # Stream died after a draft was saved. Keep that row on screen.
    if last_streaming and last:
        return {
            "task_id": task_id,
            "status": "idle",
            "content": last.get("content") or "",
            "images": last.get("images") or [],
            "error": None,
            "source": "messages",
            "hydrated": False,
        }

    # Last message is user, no live registry — wait / abandoned
    return {
        "task_id": task_id,
        "status": "pending",
        "content": "",
        "images": [],
        "error": None,
        "source": None,
        "hydrated": False,
    }
