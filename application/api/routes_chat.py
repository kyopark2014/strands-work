import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from application.api.routes_auth import _kick_graph_job, require_user_id
from application import task_store
from application.services.chat_stream_service import ChatStreamService

logger = logging.getLogger("routes_chat")

router = APIRouter(prefix="/api/tasks", tags=["chat"])

DEFAULT_IMAGE_PROMPT = "첨부한 이미지를 분석해주세요."
DEFAULT_FILE_PROMPT = "첨부한 파일을 분석해주세요."


class ChatRequest(BaseModel):
    prompt: str = ""
    files: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_prompt_or_files(self):
        if not self.prompt.strip() and not self.files:
            raise ValueError("prompt or files is required")
        return self


@router.post("/{task_id}/chat")
def chat_stream(task_id: str, body: ChatRequest, request: Request):
    user_id = require_user_id(request)
    try:
        task = task_store.get_task_refreshing(task_id, user_id)
    except Exception:
        logger.exception("Failed to load task %s", task_id)
        raise HTTPException(
            status_code=500,
            detail="Unable to load task. Please try again later.",
        )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    files = [url.strip() for url in (body.files or []) if url and url.strip()]
    prompt = body.prompt.strip()
    if not prompt and files:
        has_workspace = any(f.startswith("/mnt/workspace/") for f in files)
        has_image_url = any(not f.startswith("/mnt/workspace/") for f in files)
        if has_workspace and not has_image_url:
            prompt = DEFAULT_FILE_PROMPT
        else:
            prompt = DEFAULT_IMAGE_PROMPT

    service = ChatStreamService()
    message_queue, result_holder = service.start_chat_stream(
        task_id=task_id,
        task=task,
        user_id=user_id,
        prompt=prompt,
        files=files,
    )

    pending_graph_kick = {"yes": False}

    def on_assistant_error(safe_error: str) -> None:
        task_store.add_message(task_id, "assistant", f"Error: {safe_error}", user_id=user_id)

    def on_assistant_done(
        final_content: str,
        images: list[str],
        tool_events: list[dict[str, Any]],
    ) -> None:
        task_store.add_message(
            task_id,
            "assistant",
            final_content,
            user_id=user_id,
            images=images,
            tool_events=tool_events,
        )
        pending_graph_kick["yes"] = True

    def on_flush() -> None:
        if pending_graph_kick["yes"]:
            pending_graph_kick["yes"] = False
            _kick_graph_job(user_id)

    return StreamingResponse(
        service.iter_sse_events(
            message_queue=message_queue,
            result_holder=result_holder,
            on_assistant_error=on_assistant_error,
            on_assistant_done=on_assistant_done,
            on_flush=on_flush,
            task_id=task_id,
            user_id=user_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
