"""HTTP API for my-schedule jobs."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from application import schedule_auth
from application import schedule_service
from application import task_store
from application.services.scheduled_job_service import start_scheduled_job_async

logger = logging.getLogger("routes_schedules")

router = APIRouter(tags=["schedules"])


class CreateScheduleBody(BaseModel):
    prompt: str = Field(..., min_length=1)
    schedule_expression: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    runtime_session_id: str | None = None
    timezone: str = "Asia/Seoul"
    title: str = ""
    enabled: bool = True


class UpdateScheduleBody(BaseModel):
    prompt: str | None = None
    schedule_expression: str | None = None
    timezone: str | None = None
    title: str | None = None
    enabled: bool | None = None


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id",
        "user_id",
        "task_id",
        "runtime_session_id",
        "prompt",
        "title",
        "schedule_expression",
        "timezone",
        "enabled",
        "schedule_name",
        "schedule_arn",
        "created_at",
        "updated_at",
        "last_run_at",
        "last_run_status",
        "last_error",
    )
    return {k: job.get(k) for k in keys if k in job or job.get(k) is not None}


def _assert_task_owner(task_id: str, user_id: str) -> dict[str, Any]:
    task = task_store.get_task_refreshing(task_id, user_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/api/schedules")
def create_schedule(body: CreateScheduleBody, request: Request) -> dict[str, Any]:
    user_id = schedule_auth.require_schedule_user(request)
    task = _assert_task_owner(body.task_id, user_id)
    runtime_session_id = (
        (body.runtime_session_id or "").strip()
        or (task.get("runtime_session_id") or "").strip()
        or body.task_id
    )
    try:
        job = schedule_service.create_job(
            user_id=user_id,
            task_id=body.task_id,
            runtime_session_id=runtime_session_id,
            prompt=body.prompt,
            schedule_expression=body.schedule_expression,
            timezone=body.timezone,
            title=body.title,
            enabled=body.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception:
        logger.exception("Failed to create schedule")
        raise HTTPException(status_code=500, detail="Failed to create schedule")
    return {"ok": True, "job": _public_job(job)}


@router.get("/api/schedules")
def list_schedules(
    request: Request,
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    user_id = schedule_auth.require_schedule_user(request)
    jobs = schedule_service.list_jobs(user_id, task_id=task_id)
    return {"ok": True, "jobs": [_public_job(j) for j in jobs]}


@router.get("/api/schedules/{job_id}")
def get_schedule(job_id: str, request: Request) -> dict[str, Any]:
    user_id = schedule_auth.require_schedule_user(request)
    job = schedule_service.get_job_for_user(job_id, user_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job": _public_job(job)}


@router.patch("/api/schedules/{job_id}")
def update_schedule(
    job_id: str, body: UpdateScheduleBody, request: Request
) -> dict[str, Any]:
    user_id = schedule_auth.require_schedule_user(request)
    try:
        job = schedule_service.update_job(
            job_id,
            user_id=user_id,
            prompt=body.prompt,
            schedule_expression=body.schedule_expression,
            timezone=body.timezone,
            title=body.title,
            enabled=body.enabled,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Job not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception:
        logger.exception("Failed to update schedule %s", job_id)
        raise HTTPException(status_code=500, detail="Failed to update schedule")
    return {"ok": True, "job": _public_job(job)}


@router.delete("/api/schedules/{job_id}")
def delete_schedule(job_id: str, request: Request) -> dict[str, Any]:
    user_id = schedule_auth.require_schedule_user(request)
    try:
        schedule_service.delete_job(job_id, user_id=user_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Job not found")
    except Exception:
        logger.exception("Failed to delete schedule %s", job_id)
        raise HTTPException(status_code=500, detail="Failed to delete schedule")
    return {"ok": True, "job_id": job_id}


@router.post("/api/internal/schedules/{job_id}/run")
def run_schedule_internal(job_id: str, request: Request) -> dict[str, Any]:
    """Lambda entry: ScheduleAgent auth only. Starts job asynchronously (202)."""
    schedule_auth.require_schedule_agent(request)
    from application import schedule_store

    job = schedule_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    start_scheduled_job_async(job_id)
    return {"ok": True, "status": "accepted", "job_id": job_id}
