#!/usr/bin/env python3
"""HTTP helpers for my-schedule API (ECS app).

Auth: ScheduleAgent HMAC via ``{project}/schedule-agent-token``
(AgentCore cannot use session-signing-key).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

SCHEME = "ScheduleAgent"
VERSION = "v1"
MAX_AGE_SECONDS = 60 * 60


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_agentic_config() -> dict[str, Any]:
    env_json = (os.environ.get("APP_CONFIG_JSON") or "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    candidates: list[Path] = []
    for key in ("AGENTIC_WORK_ROOT", "WORKING_DIR", "APP_ROOT"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            candidates.append(Path(raw) / "config.json")
            candidates.append(Path(raw) / "application" / "config.json")

    here = Path(__file__).resolve()
    langgraph_root = here.parents[3]
    agentic_root = here.parents[5] if len(here.parents) > 5 else here.parents[3]
    candidates.extend(
        [
            langgraph_root / "config.json",
            agentic_root / "application" / "config.json",
            agentic_root / "config.json",
            Path.cwd() / "config.json",
            Path.cwd() / "application" / "config.json",
        ]
    )
    for path in candidates:
        if path.is_file():
            return _load_json(path)
    return {}


def _project_name(cfg: Optional[dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_agentic_config()
    return (
        (cfg.get("projectName") or os.environ.get("TASK_DB_PROJECT") or "agentic-work")
        .strip()
        or "agentic-work"
    )


def _region(cfg: Optional[dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_agentic_config()
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or (cfg.get("region") or "us-west-2")
    )


def app_base_url() -> str:
    explicit = (os.environ.get("APP_BASE_URL") or os.environ.get("SHARING_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    cfg = load_agentic_config()
    sharing = (cfg.get("sharing_url") or "").strip()
    if sharing:
        return sharing.rstrip("/")
    return "http://127.0.0.1:8501"


def resolve_user_id(cli_user_id: Optional[str] = None) -> str:
    for candidate in (
        cli_user_id,
        os.environ.get("USER_ID"),
        os.environ.get("CURRENT_USER_ID"),
        os.environ.get("AGENT_USER_ID"),
        os.environ.get("AGENTCORE_USER_ID"),
    ):
        value = (candidate or "").strip()
        if value:
            return value
    return "local-dev"


def resolve_task_id(cli_task_id: Optional[str] = None) -> Optional[str]:
    for candidate in (
        cli_task_id,
        os.environ.get("TASK_ID"),
    ):
        value = (candidate or "").strip()
        if value:
            return value
    return None


def resolve_runtime_session_id(cli: Optional[str] = None) -> Optional[str]:
    for candidate in (
        cli,
        os.environ.get("RUNTIME_SESSION_ID"),
    ):
        value = (candidate or "").strip()
        if value:
            return value
    return None


def _get_secret_string(secret_id: str) -> tuple[Optional[str], Optional[str]]:
    try:
        import boto3
    except Exception as exc:
        return None, f"boto3 unavailable: {exc}"
    try:
        client = boto3.client("secretsmanager", region_name=_region())
        response = client.get_secret_value(SecretId=secret_id)
        secret = (response.get("SecretString") or "").strip()
        if secret:
            return secret, None
        return None, f"empty secret: {secret_id}"
    except Exception as exc:
        return None, f"{secret_id}: {exc}"


def _schedule_token() -> tuple[Optional[bytes], list[str]]:
    errors: list[str] = []
    env = (os.environ.get("SCHEDULE_AGENT_TOKEN") or "").strip()
    if env:
        return env.encode("utf-8"), errors
    secret_id = f"{_project_name()}/schedule-agent-token"
    value, err = _get_secret_string(secret_id)
    if value:
        return value.encode("utf-8"), errors
    if err:
        errors.append(err)
    # Local ECS / app may share schedule_auth local file
    here = Path(__file__).resolve()
    agentic_root = here.parents[5] if len(here.parents) > 5 else Path.cwd()
    for path in (
        Path.cwd() / "application" / "data" / ".schedule_agent_token",
        agentic_root / "application" / "data" / ".schedule_agent_token",
    ):
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.encode("utf-8"), errors
    return None, errors


def _sign(user_id: str, token: bytes) -> str:
    exp = int(time.time()) + MAX_AGE_SECONDS
    payload = json.dumps(
        {"uid": user_id, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(token, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{VERSION}.{payload_b64}.{_b64encode(sig)}"


def _auth_headers(user_id: str) -> dict[str, str]:
    token, errors = _schedule_token()
    if not token:
        detail = "; ".join(errors) if errors else "no credentials"
        raise RuntimeError(
            "Schedule auth unavailable. Need SCHEDULE_AGENT_TOKEN "
            f"(Secrets Manager `{_project_name()}/schedule-agent-token`). "
            f"Details: {detail}"
        )
    return {
        "Accept": "application/json",
        "Authorization": f"{SCHEME} {_sign(user_id, token)}",
    }


def api_request(
    method: str,
    path: str,
    *,
    user_id: Optional[str] = None,
    query: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Any:
    uid = resolve_user_id(user_id)
    base = app_base_url()
    url = f"{base}{path}"
    if query:
        filtered = {k: v for k, v in query.items() if v is not None and v != ""}
        if filtered:
            url = f"{url}?{urllib.parse.urlencode(filtered)}"

    data = None
    headers = _auth_headers(uid)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {"ok": True, "status": resp.status}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = detail
        raise RuntimeError(f"HTTP {exc.code} {method.upper()} {path}: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach app at {base}: {exc}") from exc


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
