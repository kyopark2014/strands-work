"""HMAC auth for my-schedule (AgentCore skill + Lambda → ECS).

Scheme: ``Authorization: ScheduleAgent v1.<payload_b64>.<sig_b64>``
Payload JSON: ``{"uid": "<user_id>", "exp": <unix_ts>}``

AgentCore cannot read session-signing-key; it uses
``{project}/schedule-agent-token`` (same pattern as vault-agent-token).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("schedule_auth")

SCHEME = "ScheduleAgent"
VERSION = "v1"
DEFAULT_MAX_AGE_SECONDS = 60 * 60  # 1 hour for AgentCore/Lambda calls
_ENV_KEY = "SCHEDULE_AGENT_TOKEN"
_LOCAL_KEY_FILE = Path(__file__).resolve().parent / "data" / ".schedule_agent_token"

_key_lock = threading.Lock()
_cached_key: Optional[bytes] = None


def _project_name() -> str:
    try:
        try:
            from application import utils
        except ImportError:
            import utils

        cfg = utils.load_config()
        name = (cfg.get("projectName") or "").strip()
        if name:
            return name
    except Exception:
        pass
    return (os.environ.get("TASK_DB_PROJECT") or "agentic-work").strip() or "agentic-work"


def secret_name() -> str:
    return f"{_project_name()}/schedule-agent-token"


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _load_key_from_secrets_manager() -> Optional[bytes]:
    try:
        import boto3

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-west-2"
        )
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name())
        secret = (response.get("SecretString") or "").strip()
        if secret:
            return secret.encode("utf-8")
    except Exception as e:
        logger.debug("Schedule agent token not loaded from Secrets Manager: %s", e)
    return None


def _load_or_create_local_key() -> bytes:
    _LOCAL_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCAL_KEY_FILE.exists():
        existing = _LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing.encode("utf-8")
    value = secrets.token_urlsafe(32)
    _LOCAL_KEY_FILE.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(_LOCAL_KEY_FILE, 0o600)
    except OSError:
        pass
    logger.info("Created local schedule agent token at %s", _LOCAL_KEY_FILE)
    return value.encode("utf-8")


def get_token() -> bytes:
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    with _key_lock:
        if _cached_key is not None:
            return _cached_key
        env_key = (os.environ.get(_ENV_KEY) or "").strip()
        if env_key:
            _cached_key = env_key.encode("utf-8")
            return _cached_key
        sm_key = _load_key_from_secrets_manager()
        if sm_key:
            _cached_key = sm_key
            return _cached_key
        _cached_key = _load_or_create_local_key()
        return _cached_key


def reset_token_cache() -> None:
    global _cached_key
    with _key_lock:
        _cached_key = None


def sign(user_id: str, *, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> str:
    uid = (user_id or "").strip() or "schedule-runner"
    exp = int(time.time()) + int(max_age_seconds)
    payload = json.dumps(
        {"uid": uid, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(get_token(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{VERSION}.{payload_b64}.{_b64encode(sig)}"


def authorization_header(user_id: str) -> str:
    return f"{SCHEME} {sign(user_id)}"


def verify(credential: str) -> Optional[str]:
    """Return uid if valid ScheduleAgent credential; otherwise None."""
    raw = (credential or "").strip()
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        return None
    _, payload_b64, sig_b64 = parts
    try:
        expected = hmac.new(
            get_token(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        provided = _b64decode(sig_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    uid = (payload.get("uid") or "").strip()
    exp = payload.get("exp")
    if not uid or not isinstance(exp, int):
        return None
    if exp < int(time.time()):
        return None
    return uid


def extract_schedule_agent_user(request: Request) -> Optional[str]:
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.lower().startswith(f"{SCHEME.lower()} "):
        return None
    return verify(auth[len(SCHEME) + 1 :].strip())


def require_schedule_agent(request: Request) -> str:
    """Require ScheduleAgent auth (Lambda / skill). Returns uid."""
    uid = extract_schedule_agent_user(request)
    if not uid:
        raise HTTPException(status_code=401, detail="ScheduleAgent auth required")
    return uid


def require_schedule_user(request: Request) -> str:
    """Accept ScheduleAgent or session cookie. Returns user_id."""
    uid = extract_schedule_agent_user(request)
    if uid and uid != "schedule-runner":
        return uid
    try:
        from application.api.routes_auth import get_optional_user_id
    except ImportError:
        from api.routes_auth import get_optional_user_id  # type: ignore

    session_uid = get_optional_user_id(request)
    if session_uid:
        return session_uid
    if uid == "schedule-runner":
        raise HTTPException(
            status_code=401,
            detail="ScheduleAgent uid required for user operations",
        )
    raise HTTPException(status_code=401, detail="User session or ScheduleAgent required")
