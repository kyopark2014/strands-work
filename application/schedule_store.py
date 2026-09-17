"""DynamoDB persistence for my-schedule jobs."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger("schedule_store")

_USER_GSI = "user_id-created_at-index"
_TASK_GSI = "task_id-created_at-index"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def table_name() -> str:
    env = (os.environ.get("SCHEDULE_JOBS_TABLE") or "").strip()
    if env:
        return env
    try:
        from application import utils
    except ImportError:
        import utils  # type: ignore

    cfg = utils.load_config()
    name = (cfg.get("schedule_jobs_table") or "").strip()
    if name:
        return name
    project = (cfg.get("projectName") or "agentic-work").strip() or "agentic-work"
    return f"dynamodb-{project}-schedules"


def _table():
    return boto3.resource("dynamodb", region_name=_region()).Table(table_name())


def put_job(job: dict[str, Any]) -> dict[str, Any]:
    item = dict(job)
    item.setdefault("created_at", _now_iso())
    item["updated_at"] = _now_iso()
    _table().put_item(Item=item)
    return item


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    jid = (job_id or "").strip()
    if not jid:
        return None
    resp = _table().get_item(Key={"job_id": jid})
    item = resp.get("Item")
    return item if isinstance(item, dict) else None


def delete_job(job_id: str) -> bool:
    jid = (job_id or "").strip()
    if not jid:
        return False
    _table().delete_item(Key={"job_id": jid})
    return True


def update_job(job_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    existing = get_job(job_id)
    if not existing:
        return None
    for key, value in fields.items():
        if value is not None:
            existing[key] = value
    existing["updated_at"] = _now_iso()
    _table().put_item(Item=existing)
    return existing


def list_jobs_for_user(
    user_id: str, *, task_id: Optional[str] = None, limit: int = 50
) -> list[dict[str, Any]]:
    uid = (user_id or "").strip()
    if not uid:
        return []
    tid = (task_id or "").strip()
    table = _table()
    if tid:
        resp = table.query(
            IndexName=_TASK_GSI,
            KeyConditionExpression=Key("task_id").eq(tid),
            ScanIndexForward=False,
            Limit=max(1, min(limit, 100)),
        )
        items = [
            i
            for i in (resp.get("Items") or [])
            if isinstance(i, dict) and i.get("user_id") == uid
        ]
        return items

    resp = table.query(
        IndexName=_USER_GSI,
        KeyConditionExpression=Key("user_id").eq(uid),
        ScanIndexForward=False,
        Limit=max(1, min(limit, 100)),
    )
    return [i for i in (resp.get("Items") or []) if isinstance(i, dict)]


def scan_all_jobs(*, limit: int = 500) -> list[dict[str, Any]]:
    """Scan all schedule jobs (startup cleanup). Caps pages for safety."""
    table = _table()
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Limit": min(100, max(1, limit))}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items") or []:
            if isinstance(item, dict):
                items.append(item)
                if len(items) >= limit:
                    return items
        token = resp.get("LastEvaluatedKey")
        if not token:
            break
        kwargs["ExclusiveStartKey"] = token
    return items


def mark_run(
    job_id: str,
    *,
    status: str,
    error: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    return update_job(
        job_id,
        last_run_at=_now_iso(),
        last_run_status=status,
        last_error=error or "",
    )
