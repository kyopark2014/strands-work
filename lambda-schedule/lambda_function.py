"""EventBridge Scheduler → Lambda → ECS scheduled job runner.

Event input: ``{"job_id": "<uuid>"}``
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "").rstrip("/")
SCHEDULE_AGENT_TOKEN_SECRET = os.environ.get(
    "SCHEDULE_AGENT_TOKEN_SECRET", ""
).strip()
JOBS_TABLE = (os.environ.get("SCHEDULE_JOBS_TABLE") or "").strip()
AWS_REGION = (
    os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-west-2"
)


def _token() -> str:
    env = (os.environ.get("SCHEDULE_AGENT_TOKEN") or "").strip()
    if env:
        return env
    secret_id = SCHEDULE_AGENT_TOKEN_SECRET or ""
    if not secret_id:
        raise RuntimeError("SCHEDULE_AGENT_TOKEN or SCHEDULE_AGENT_TOKEN_SECRET required")
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    resp = client.get_secret_value(SecretId=secret_id)
    return (resp.get("SecretString") or "").strip()


def _sign_auth(user_id: str = "schedule-runner") -> str:
    import base64
    import hashlib
    import hmac
    import time

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    token = _token().encode("utf-8")
    exp = int(time.time()) + 3600
    payload = json.dumps(
        {"uid": user_id, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = b64(payload.encode("utf-8"))
    sig = hmac.new(token, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"ScheduleAgent v1.{payload_b64}.{b64(sig)}"


def _get_job(job_id: str) -> dict[str, Any] | None:
    if not JOBS_TABLE:
        return None
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(JOBS_TABLE)
    resp = table.get_item(Key={"job_id": job_id})
    item = resp.get("Item")
    return item if isinstance(item, dict) else None


def _call_ecs(job_id: str) -> dict[str, Any]:
    if not APP_BASE_URL:
        raise RuntimeError("APP_BASE_URL is not configured")
    url = f"{APP_BASE_URL}/api/internal/schedules/{job_id}/run"
    headers = {
        "Authorization": _sign_auth(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        url, data=b"{}", headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ECS HTTP {exc.code}: {detail}") from exc


def handler(event, context):
    logger.info("event=%s", json.dumps(event, default=str)[:2000])
    job_id = ""
    if isinstance(event, dict):
        job_id = str(event.get("job_id") or "").strip()
        # Scheduler may wrap input
        if not job_id and isinstance(event.get("detail"), dict):
            job_id = str(event["detail"].get("job_id") or "").strip()
    if not job_id:
        return {"ok": False, "error": "job_id missing"}

    job = _get_job(job_id)
    if job is None and JOBS_TABLE:
        logger.error("Job not found in DynamoDB: %s", job_id)
        return {"ok": False, "error": "job not found", "job_id": job_id}
    if job and not job.get("enabled", True):
        logger.info("Job disabled, skip: %s", job_id)
        return {"ok": True, "status": "skipped", "job_id": job_id}

    result = _call_ecs(job_id)
    logger.info("ECS result=%s", result)
    return {"ok": True, "job_id": job_id, "ecs": result}
