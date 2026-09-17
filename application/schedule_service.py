"""Create / update / delete EventBridge Scheduler schedules for my-schedule jobs."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from application import schedule_store

logger = logging.getLogger("schedule_service")

_CRON_RE = re.compile(r"^cron\(.+\)$", re.IGNORECASE)
_RATE_RE = re.compile(r"^rate\(\d+\s+(minute|minutes|hour|hours|day|days)\)$", re.IGNORECASE)


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _cfg() -> dict[str, Any]:
    try:
        from application import utils
    except ImportError:
        import utils  # type: ignore

    return utils.load_config()


def schedule_group_name() -> str:
    env = (os.environ.get("SCHEDULE_GROUP_NAME") or "").strip()
    if env:
        return env
    name = (_cfg().get("schedule_group_name") or "").strip()
    if name:
        return name
    project = (_cfg().get("projectName") or "agentic-work").strip() or "agentic-work"
    return f"schedule-group-{project}"


def lambda_arn() -> str:
    env = (os.environ.get("SCHEDULE_LAMBDA_ARN") or "").strip()
    if env:
        return env
    return (_cfg().get("schedule_lambda_arn") or "").strip()


def scheduler_role_arn() -> str:
    env = (os.environ.get("SCHEDULER_ROLE_ARN") or "").strip()
    if env:
        return env
    return (_cfg().get("scheduler_role_arn") or "").strip()


def _scheduler():
    return boto3.client("scheduler", region_name=_region())


def normalize_schedule_expression(expression: str) -> str:
    raw = (expression or "").strip()
    if not raw:
        raise ValueError("schedule_expression is required")
    # Allow bare cron fields: "0 8 * * ? *" → cron(...)
    if not raw.lower().startswith("cron(") and not raw.lower().startswith("rate("):
        parts = raw.split()
        if len(parts) == 6:
            raw = f"cron({raw})"
        elif len(parts) == 5:
            # user-style "m h dom mon dow" → EventBridge needs year + ? for dow/dom
            m, h, dom, mon, dow = parts
            if dow != "?" and dom == "*":
                dom = "?"
            elif dom != "?" and dow == "*":
                dow = "?"
            raw = f"cron({m} {h} {dom} {mon} {dow} *)"
    if not (_CRON_RE.match(raw) or _RATE_RE.match(raw)):
        raise ValueError(
            f"Invalid schedule_expression: {expression!r}. "
            "Use cron(0 8 * * ? *) or rate(1 hours)."
        )
    return raw


def schedule_name_for(job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", job_id)[:48]
    project = (_cfg().get("projectName") or "agentic-work").strip() or "agentic-work"
    prefix = re.sub(r"[^A-Za-z0-9_-]", "-", project)[:12]
    return f"{prefix}-job-{safe}"[:64]


def _ensure_scheduler_ready() -> tuple[str, str]:
    larn = lambda_arn()
    rarn = scheduler_role_arn()
    if not larn or not rarn:
        raise RuntimeError(
            "Schedule infra not configured. "
            "Need schedule_lambda_arn and scheduler_role_arn "
            "(run installer or set SCHEDULE_LAMBDA_ARN / SCHEDULER_ROLE_ARN)."
        )
    return larn, rarn


def create_job(
    *,
    user_id: str,
    task_id: str,
    runtime_session_id: str,
    prompt: str,
    schedule_expression: str,
    timezone: str = "Asia/Seoul",
    title: str = "",
    enabled: bool = True,
) -> dict[str, Any]:
    uid = (user_id or "").strip()
    tid = (task_id or "").strip()
    sid = (runtime_session_id or "").strip() or tid
    text = (prompt or "").strip()
    if not uid or not tid or not text:
        raise ValueError("user_id, task_id, and prompt are required")

    expr = normalize_schedule_expression(schedule_expression)
    tz = (timezone or "Asia/Seoul").strip() or "Asia/Seoul"
    job_id = str(uuid.uuid4())
    name = schedule_name_for(job_id)
    larn, rarn = _ensure_scheduler_ready()
    group = schedule_group_name()
    state = "ENABLED" if enabled else "DISABLED"

    create_resp = _scheduler().create_schedule(
        Name=name,
        GroupName=group,
        ScheduleExpression=expr,
        ScheduleExpressionTimezone=tz,
        FlexibleTimeWindow={"Mode": "OFF"},
        State=state,
        Target={
            "Arn": larn,
            "RoleArn": rarn,
            "Input": json.dumps({"job_id": job_id}),
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": 3600,
                "MaximumRetryAttempts": 2,
            },
        },
        Description=(title or text)[:512],
    )

    schedule_arn = (create_resp.get("ScheduleArn") or "").strip() or (
        f"arn:aws:scheduler:{_region()}:{_cfg().get('accountId', '')}"
        f":schedule/{group}/{name}"
    )
    job = {
        "job_id": job_id,
        "user_id": uid,
        "task_id": tid,
        "runtime_session_id": sid,
        "prompt": text,
        "title": (title or text[:80]).strip(),
        "schedule_expression": expr,
        "timezone": tz,
        "enabled": bool(enabled),
        "schedule_name": name,
        "schedule_group": group,
        "schedule_arn": schedule_arn,
        "last_run_at": "",
        "last_run_status": "",
        "last_error": "",
    }
    return schedule_store.put_job(job)


def update_job(
    job_id: str,
    *,
    user_id: str,
    prompt: Optional[str] = None,
    schedule_expression: Optional[str] = None,
    timezone: Optional[str] = None,
    title: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> dict[str, Any]:
    existing = schedule_store.get_job(job_id)
    if not existing or existing.get("user_id") != user_id:
        raise LookupError("job not found")

    new_prompt = prompt if prompt is not None else existing.get("prompt")
    new_expr = (
        normalize_schedule_expression(schedule_expression)
        if schedule_expression is not None
        else existing.get("schedule_expression")
    )
    new_tz = timezone if timezone is not None else existing.get("timezone", "Asia/Seoul")
    new_title = title if title is not None else existing.get("title")
    new_enabled = existing.get("enabled", True) if enabled is None else bool(enabled)

    name = existing.get("schedule_name") or schedule_name_for(job_id)
    group = existing.get("schedule_group") or schedule_group_name()
    larn, rarn = _ensure_scheduler_ready()
    state = "ENABLED" if new_enabled else "DISABLED"

    _scheduler().update_schedule(
        Name=name,
        GroupName=group,
        ScheduleExpression=new_expr,
        ScheduleExpressionTimezone=new_tz,
        FlexibleTimeWindow={"Mode": "OFF"},
        State=state,
        Target={
            "Arn": larn,
            "RoleArn": rarn,
            "Input": json.dumps({"job_id": job_id}),
            "RetryPolicy": {
                "MaximumEventAgeInSeconds": 3600,
                "MaximumRetryAttempts": 2,
            },
        },
        Description=(new_title or new_prompt or "")[:512],
    )

    updated = schedule_store.update_job(
        job_id,
        prompt=new_prompt,
        schedule_expression=new_expr,
        timezone=new_tz,
        title=new_title,
        enabled=new_enabled,
        schedule_name=name,
        schedule_group=group,
    )
    if not updated:
        raise LookupError("job not found after update")
    return updated


def delete_job(job_id: str, *, user_id: str) -> bool:
    existing = schedule_store.get_job(job_id)
    if not existing or existing.get("user_id") != user_id:
        raise LookupError("job not found")

    name = existing.get("schedule_name") or schedule_name_for(job_id)
    group = existing.get("schedule_group") or schedule_group_name()
    try:
        _scheduler().delete_schedule(Name=name, GroupName=group)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in {"ResourceNotFoundException", "ScheduleNotFound"}:
            raise
        logger.warning("Schedule already gone: %s/%s", group, name)

    return schedule_store.delete_job(job_id)


def get_job_for_user(job_id: str, user_id: str) -> Optional[dict[str, Any]]:
    job = schedule_store.get_job(job_id)
    if not job or job.get("user_id") != user_id:
        return None
    return job


def list_jobs(user_id: str, *, task_id: Optional[str] = None) -> list[dict[str, Any]]:
    return schedule_store.list_jobs_for_user(user_id, task_id=task_id)


def _purge_job_record(job: dict[str, Any]) -> None:
    """Delete EventBridge schedule (if any) + DynamoDB row. Best-effort."""
    job_id = (job.get("job_id") or "").strip()
    if not job_id:
        return
    name = job.get("schedule_name") or schedule_name_for(job_id)
    group = job.get("schedule_group") or schedule_group_name()
    try:
        _scheduler().delete_schedule(Name=name, GroupName=group)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in {"ResourceNotFoundException", "ScheduleNotFound"}:
            logger.warning("Failed to delete schedule %s/%s: %s", group, name, e)
    except Exception as e:
        logger.warning("Scheduler delete failed for %s: %s", job_id, e)
    schedule_store.delete_job(job_id)


def _parse_cron_fields(expression: str) -> Optional[tuple[str, str, str, str, str, str]]:
    raw = (expression or "").strip()
    if not raw.lower().startswith("cron("):
        return None
    inner = raw[raw.find("(") + 1 : raw.rfind(")")].strip()
    parts = inner.split()
    if len(parts) != 6:
        return None
    return parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]


def _one_time_fire_at(
    expression: str, timezone_name: str
) -> Optional[datetime]:
    """Return fire datetime for one-shot cron/at expressions; else None (recurring)."""
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    raw = (expression or "").strip()
    tz_name = (timezone_name or "Asia/Seoul").strip() or "Asia/Seoul"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    # at(2026-09-17T18:05:00)
    if raw.lower().startswith("at("):
        inner = raw[raw.find("(") + 1 : raw.rfind(")")].strip().strip("'\"")
        try:
            when = dt.fromisoformat(inner)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        return when.astimezone(timezone.utc)

    fields = _parse_cron_fields(raw)
    if not fields:
        return None
    minute, hour, day, month, _dow, year = fields
    # One-shot: specific year + month + day-of-month
    if not (year.isdigit() and month.isdigit() and day.isdigit()):
        return None
    if not (minute.isdigit() and hour.isdigit()):
        return None
    try:
        when = dt(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            0,
            tzinfo=tz,
        )
    except ValueError:
        return None
    return when.astimezone(timezone.utc)


def schedule_has_future_runs(job: dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    """False when the job can no longer fire (completed one-shot / missing schedule)."""
    from datetime import datetime as dt

    expr = (job.get("schedule_expression") or "").strip()
    if not expr:
        return False
    if _RATE_RE.match(expr):
        return True

    now_utc = now or dt.now(timezone.utc)
    fire_at = _one_time_fire_at(expr, job.get("timezone") or "Asia/Seoul")
    if fire_at is not None:
        # Allow a short grace after fire time for in-flight Lambda
        return now_utc < fire_at

    # Recurring cron (year=* or wildcards) — still active
    fields = _parse_cron_fields(expr)
    if fields:
        year = fields[5]
        if year == "*" or "," in year or "-" in year or "/" in year:
            return True
        # Specific year range already past
        if year.isdigit() and int(year) < now_utc.year:
            return False
        return True

    # Unknown expression: keep unless EventBridge schedule is gone
    return True


def _scheduler_schedule_exists(job: dict[str, Any]) -> bool:
    name = job.get("schedule_name") or schedule_name_for(job.get("job_id") or "")
    group = job.get("schedule_group") or schedule_group_name()
    if not name:
        return False
    try:
        _scheduler().get_schedule(Name=name, GroupName=group)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"ResourceNotFoundException", "ScheduleNotFound"}:
            return False
        logger.warning("get_schedule failed for %s/%s: %s", group, name, e)
        return True  # fail closed — don't delete on transient errors
    except Exception as e:
        logger.warning("get_schedule error for %s: %s", name, e)
        return True


def cleanup_completed_schedules() -> dict[str, int]:
    """Remove jobs with no future runs (one-shot past / Scheduler already gone)."""
    deleted = 0
    kept = 0
    errors = 0
    try:
        jobs = schedule_store.scan_all_jobs()
    except Exception:
        logger.exception("Failed to scan schedule jobs for cleanup")
        return {"deleted": 0, "kept": 0, "errors": 1}

    for job in jobs:
        job_id = job.get("job_id")
        try:
            if schedule_has_future_runs(job):
                if _scheduler_schedule_exists(job):
                    kept += 1
                    continue
                logger.info(
                    "Purging orphan schedule job (Scheduler missing): %s", job_id
                )
            else:
                logger.info(
                    "Purging completed schedule job (no future runs): %s expr=%s",
                    job_id,
                    job.get("schedule_expression"),
                )
            _purge_job_record(job)
            deleted += 1
        except Exception:
            errors += 1
            logger.exception("Failed to cleanup schedule job %s", job_id)

    logger.info(
        "Schedule cleanup done: deleted=%s kept=%s errors=%s",
        deleted,
        kept,
        errors,
    )
    return {"deleted": deleted, "kept": kept, "errors": errors}
