import logging
import os
import re
from urllib import parse

from strands import tool

import utils
import tools.workspace as workspace
from tools.workspace import WORKING_DIR

logger = logging.getLogger("strands-agent")

# CloudFront signed-cookie behaviors cover these prefixes only.
_ALLOWED_KEY_PREFIXES = ("artifacts/", "images/", "docs/")
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._\-/= ]+")


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
    """Current chat user segment for S3 artifact keys (artifacts/{user}/…)."""
    try:
        import chat

        return (
            workspace.sanitize_user_path_segment(getattr(chat, "user_id", None))
            or "default"
        )
    except Exception:
        return "default"


def _artifacts_object_key(basename: str) -> str:
    return f"artifacts/{_artifact_user_segment()}/{basename}"


def build_s3_key(filepath: str, *, content_type: str = "") -> str:
    """Build an S3 object key safe for PutObject and CloudFront sharing.

    Agent tools sometimes pass paths like ``../../app/contents/foo.png``. S3
    rejects keys containing ``..`` (400 Bad Request), and CloudFront only
    serves ``/artifacts/*``, ``/images/*``, and ``/docs/*``.

    Artifact keys are ``artifacts/{user_id}/{filename}`` so each user's files
    are isolated (viewer auth matches the same layout).
    """
    normalized = os.path.normpath(filepath.replace("\\", "/")).lstrip("/")
    # Drop leading ../ segments after normpath of relative inputs.
    while normalized.startswith("../"):
        normalized = normalized[3:]
    if normalized in (".", "..", ""):
        normalized = _safe_basename(filepath)

    lower = normalized.lower()
    basename = _safe_basename(normalized)
    user_seg = _artifact_user_segment()

    if lower.startswith("application/artifacts/"):
        return _artifacts_object_key(
            _safe_basename(normalized[len("application/artifacts/") :])
        )
    if lower.startswith("artifacts/"):
        rest = normalized.split("/", 1)[1] if "/" in normalized else basename
        rest_parts = [p for p in rest.split("/") if p]
        # Avoid artifacts/{user}/{user}/... when caller already included user_id.
        if rest_parts and rest_parts[0] == user_seg:
            leaf = _safe_basename("/".join(rest_parts[1:]) or rest_parts[0])
            return f"artifacts/{user_seg}/{leaf}"
        return _artifacts_object_key(_safe_basename(rest))
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
        # Prefer artifacts for agent-produced files when path was ambiguous.
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
        s3_key = build_s3_key(filepath, content_type=content_type)
        if not s3_key.startswith(_ALLOWED_KEY_PREFIXES):
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
