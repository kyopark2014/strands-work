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

import logging
import os
import re
from urllib import parse

from strands import tool

import utils
import tools.workspace as workspace
from tools.workspace import WORKING_DIR

logger = logging.getLogger("strands-agent")

# CloudFront signed behaviors: /images/*, /docs/*, /artifacts/* (legacy copies),
# and /*/artifacts/* for the workspace layout {user}/artifacts/.
_ALLOWED_KEY_PREFIXES = ("artifacts/", "images/", "docs/")
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._\-/= ]+")
_WORKSPACE_MOUNT = "/mnt/workspace"


def s3_uri_to_console_url(uri: str, region: str) -> str:
    """Open the object in the AWS S3 console (when sharing_url is not configured)."""
    if not uri or not uri.startswith("s3://"):
        return ""
    rest = uri[5:]
    parts = rest.split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    enc_key = parse.quote(key, safe="")
    return f"https://{region}.console.aws.amazon.com/s3/object/{bucket}?prefix={enc_key}"


def resolve_workspace_path(filepath: str) -> str:
    """Resolve workspace-relative paths for artifacts and application files."""
    if os.path.isabs(filepath):
        return os.path.normpath(filepath)
    normalized = filepath.replace("\\", "/")
    if (
        normalized in ("artifacts", "application/artifacts")
        or normalized.startswith("artifacts/")
        or normalized.startswith("application/artifacts/")
    ):
        if normalized.startswith("application/artifacts"):
            suffix = normalized[len("application/artifacts"):].lstrip("/")
        else:
            suffix = normalized[len("artifacts"):].lstrip("/")
        return os.path.join(workspace.ARTIFACTS_DIR, suffix) if suffix else workspace.ARTIFACTS_DIR
    return os.path.normpath(os.path.join(WORKING_DIR, filepath))


def _safe_basename(filepath: str) -> str:
    name = os.path.basename(filepath.replace("\\", "/").rstrip("/")) or "upload.bin"
    # Strip path traversal leftovers and odd characters from the filename only.
    name = name.replace("..", "_").strip("._") or "upload.bin"
    return _UNSAFE_KEY_CHARS.sub("_", name)


def _artifact_user_segment() -> str:
    """Current chat user segment for S3 artifact keys ({user}/artifacts/…)."""
    try:
        import chat

        return (
            workspace.sanitize_user_path_segment(getattr(chat, "user_id", None))
            or "default"
        )
    except Exception:
        return "default"


def _safe_relative_key(relative: str) -> str:
    """Keep nested folders; drop traversal and unsafe characters."""
    parts: list[str] = []
    for part in relative.replace("\\", "/").split("/"):
        if part in ("", ".", ".."):
            continue
        cleaned = _UNSAFE_KEY_CHARS.sub("_", part.replace("..", "_")).strip(" .")
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts)


def _suffix_after_artifacts_segment(normalized: str) -> str | None:
    """Return the path after an ``artifacts`` segment, or None if absent."""
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    for index, part in enumerate(parts):
        if part.lower() == "artifacts":
            return "/".join(parts[index + 1 :])
    return None


def _artifacts_object_key(relative: str, *, strip_user_prefix: bool = False) -> str:
    """Canonical object key: ``{user}/artifacts/{relative}`` (nested path kept).

    ``strip_user_prefix`` drops a leading user segment from agent paths such as
    ``artifacts/{user}/file`` so the key is not ``{user}/artifacts/{user}/file``.
    """
    suffix = _safe_relative_key(relative)
    user = _artifact_user_segment()
    if strip_user_prefix and suffix:
        parts = suffix.split("/")
        if parts[0] == user:
            suffix = "/".join(parts[1:])
    suffix = suffix or "file.bin"
    return f"{user}/artifacts/{suffix}"


def is_shareable_s3_key(key: str) -> bool:
    """True for CloudFront-served keys, including ``{user}/artifacts/…``."""
    lower = (key or "").lower()
    if lower.startswith(_ALLOWED_KEY_PREFIXES):
        return True
    parts = [p for p in lower.split("/") if p]
    return len(parts) >= 3 and parts[1] == "artifacts"


def workspace_mount_object_key(full_path: str) -> str | None:
    """S3 key when ``full_path`` is already an object on the workspace mount.

    ``/mnt/workspace/{user}/artifacts/…`` is the bucket object. Uploading it
    again would write a second key.
    """
    if not full_path or not os.path.isdir(_WORKSPACE_MOUNT):
        return None
    real = os.path.realpath(full_path)
    mount = os.path.realpath(_WORKSPACE_MOUNT)
    try:
        if os.path.commonpath([real, mount]) != mount:
            return None
    except ValueError:
        return None
    rel = os.path.relpath(real, mount).replace(os.sep, "/")
    if rel in ("", ".") or rel.startswith("../"):
        return None
    return rel


def build_s3_key(filepath: str, *, content_type: str = "") -> str:
    """Build an S3 object key safe for PutObject and CloudFront sharing.

    Agent tools sometimes pass paths like ``../../app/contents/foo.png``. S3
    rejects keys containing ``..`` (400 Bad Request). CloudFront serves
    ``/images/*``, ``/docs/*``, legacy ``/artifacts/*``, and ``/*/artifacts/*``.

    Workspace artifacts stay at ``{user}/artifacts/{relative}``. Callers that
    already wrote the file on the S3 Files mount must not upload a second copy.
    """
    normalized = os.path.normpath(filepath.replace("\\", "/")).lstrip("/")
    # Drop leading ../ segments after normpath of relative inputs.
    while normalized.startswith("../"):
        normalized = normalized[3:]
    if normalized in (".", "..", ""):
        normalized = _safe_basename(filepath)

    lower = normalized.lower()
    basename = _safe_basename(normalized)

    artifact_suffix = _suffix_after_artifacts_segment(normalized)
    if artifact_suffix is not None:
        # Agent-relative artifacts/{user}/… only. Mount paths already contain
        # {user}/artifacts/ before the suffix, so do not strip again.
        agent_relative = lower.startswith("artifacts") or lower.startswith(
            "application/artifacts"
        )
        return _artifacts_object_key(
            artifact_suffix, strip_user_prefix=agent_relative
        )
    if lower.startswith("images/") or lower.startswith("application/images/"):
        return f"images/{basename}"
    if lower.startswith("docs/") or lower.startswith("application/docs/"):
        return f"docs/{basename}"
    if lower.startswith("contents/") or "/contents/" in lower:
        # Generated charts/images under contents/ → CloudFront /images/*
        if content_type.startswith("image/") or basename.lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp")
        ):
            return f"images/{basename}"
        return f"docs/{basename}"

    if content_type.startswith("image/") or basename.lower().endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp")
    ):
        return f"images/{basename}"

    # Default non-image uploads (reports, etc.)
    if any(
        basename.lower().endswith(ext)
        for ext in (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".txt", ".md")
    ):
        # Prefer the workspace artifact tree when the path was ambiguous.
        return _artifacts_object_key(basename)

    return f"docs/{basename}"


def _is_under_workspace(full_path: str) -> bool:
    real = os.path.realpath(full_path)
    roots = (
        os.path.realpath(WORKING_DIR),
        os.path.realpath(workspace.ARTIFACTS_DIR),
    )
    return any(real == root or real.startswith(root + os.sep) for root in roots)


@tool
def upload_file_to_s3(filepath: str) -> str:
    """Upload a local file to S3 and return the download URL.

    Args:
        filepath: Path under application/ (e.g. 'artifacts/report.pdf' or 'application/artifacts/report.pdf').

    Returns:
        The download URL, or an error message.
    """
    logger.info(f"###### upload_file_to_s3: {filepath} ######")
    try:
        import boto3
        from urllib import parse as url_parse

        s3_bucket = utils.get_s3_bucket()
        if not s3_bucket:
            return "S3 bucket is not configured."

        full_path = resolve_workspace_path(filepath)
        if not os.path.exists(full_path):
            return f"File not found: {filepath}"
        if not _is_under_workspace(full_path):
            logger.warning("Rejected upload outside workspace: %r -> %r", filepath, full_path)
            return f"File not found: {filepath}"

        content_type = utils.get_contents_type(filepath)
        mount_key = workspace_mount_object_key(full_path)
        if mount_key and is_shareable_s3_key(mount_key):
            s3_key = mount_key
            logger.info("artifact already on workspace mount: %s", s3_key)
        else:
            s3_key = build_s3_key(filepath, content_type=content_type)
            if not is_shareable_s3_key(s3_key):
                s3_key = f"docs/{_safe_basename(s3_key)}"

            region = utils.get_aws_region()
            logger.info("S3 put_object key=%s (from filepath=%r)", s3_key, filepath)
            s3 = boto3.client("s3", region_name=region)

            put_params = {
                "Bucket": s3_bucket,
                "Key": s3_key,
            }
            if content_type and content_type != "no info":
                put_params["ContentType"] = content_type

            with open(full_path, "rb") as f:
                put_params["Body"] = f.read()
                s3.put_object(**put_params)

        region = utils.get_aws_region()

        sharing_url = utils.get_sharing_url()
        if sharing_url:
            url = f"{sharing_url}/{url_parse.quote(s3_key)}"
            return f"Upload complete: {url}"
        return (
            "Upload complete: "
            f"{s3_uri_to_console_url(f's3://{s3_bucket}/{s3_key}', region)}"
        )

    except Exception:
        logger.error("S3 upload failed for %r", filepath, exc_info=True)
        return "Upload failed: S3 operation error"
