"""S3 helpers for the chat module. Imports chat for shared module state."""

import logging
import os

import utils
from botocore.exceptions import ClientError
from urllib import parse

from aws_client_factory import create_boto3_client, create_boto3_resource

logger = logging.getLogger("chat")


def get_s3_client(region_name=None):
    """Create an S3 client using env credentials when present, else default chain."""
    import chat

    region = region_name or chat.bedrock_region
    return create_boto3_client("s3", region_name=region)


def get_s3_resource(region_name=None):
    """Create an S3 resource using env credentials when present, else default chain."""
    import chat

    region = region_name or chat.bedrock_region
    return create_boto3_resource("s3", region_name=region)


def create_object(key, body):
    """
    Create an object in S3 and return the URL. If the file already exists, append the new content.
    """
    import chat

    # Content-Type based on file extension
    content_type = "application/octet-stream"  # default value
    if key.endswith(".html"):
        content_type = "text/html"
    elif key.endswith(".md"):
        content_type = "text/markdown"

    s3_client = get_s3_client()

    try:
        s3_client.put_object(
            Bucket=chat.s3_bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    except ClientError as e:
        logger.error(f"S3 put_object failed for key={key}: {e}", exc_info=True)
        raise Exception("Failed to store object in S3") from e
    except Exception as e:
        logger.error(f"S3 put_object failed for key={key}: {e}", exc_info=True)
        raise Exception("Failed to store object in S3") from e


def updata_object(key, body, direction):
    """
    Create an object in S3 and return the URL. If the file already exists, append the new content.
    """
    import chat

    s3_client = get_s3_client()

    try:
        # Check if file exists
        try:
            response = s3_client.get_object(Bucket=chat.s3_bucket, Key=key)
            existing_body = response["Body"].read().decode("utf-8")
            # Append new content to existing content

            if direction == "append":
                updated_body = existing_body + "\n" + body
            else:  # prepend
                updated_body = body + "\n" + existing_body
        except s3_client.exceptions.NoSuchKey:
            # File doesn't exist, use new body as is
            updated_body = body

        # Content-Type based on file extension
        content_type = "application/octet-stream"  # default value
        if key.endswith(".html"):
            content_type = "text/html"
        elif key.endswith(".md"):
            content_type = "text/markdown"

        # Upload the updated content
        s3_client.put_object(
            Bucket=chat.s3_bucket,
            Key=key,
            Body=updated_body,
            ContentType=content_type,
        )

    except Exception as e:
        logger.error("Error updating object in S3: %s", type(e).__name__, exc_info=True)
        raise Exception("Failed to update object in S3") from e


def upload_to_s3(file_bytes, file_name):
    """
    Upload a file to S3 under {prefix}/{user_id}/ and return the URL.
    """
    import chat
    from tools.workspace import sanitize_user_path_segment

    try:
        s3_client = get_s3_client()

        content_type = utils.get_contents_type(file_name)
        logger.info(f"content_type: {content_type}")

        if content_type.startswith("image/"):
            prefix = chat.s3_image_prefix
        else:
            prefix = chat.s3_prefix

        user_segment = sanitize_user_path_segment(chat.user_id)
        if user_segment:
            s3_key = f"{prefix}/{user_segment}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(user_segment)}/{parse.quote(file_name)}"
        else:
            s3_key = f"{prefix}/{file_name}"
            relative_url_path = f"{prefix}/{parse.quote(file_name)}"

        user_meta = {  # user-defined metadata
            "content_type": content_type,
            "model_name": chat.model_name,
        }

        response = s3_client.put_object(
            Bucket=chat.s3_bucket,
            Key=s3_key,
            ContentType=content_type,
            Metadata=user_meta,
            Body=file_bytes,
        )
        logger.info(f"upload response: {response}")

        base = (chat.path or "").rstrip("/")
        url = f"{base}/{relative_url_path}" if base else relative_url_path
        return url

    except Exception as e:
        logger.error("Error uploading to S3: %s", type(e).__name__, exc_info=True)
        return None


def upload_to_s3_artifacts(file_bytes, file_name):
    """
    Upload a file to S3 and return the URL
    """
    import chat

    try:
        s3_client = get_s3_client()

        content_type = utils.get_contents_type(file_name)
        logger.info(f"content_type: {content_type}")

        from tools.workspace import sanitize_user_path_segment

        user_seg = sanitize_user_path_segment(getattr(chat, "user_id", None)) or "default"
        safe_name = os.path.basename(file_name) or file_name
        s3_key = f"artifacts/{user_seg}/{safe_name}"

        user_meta = {  # user-defined metadata
            "content_type": content_type,
            "model_name": chat.model_name,
        }

        response = s3_client.put_object(
            Bucket=chat.s3_bucket,
            Key=s3_key,
            ContentType=content_type,
            Metadata=user_meta,
            Body=file_bytes,
        )
        logger.info(f"upload response: {response}")

        url = (
            chat.path
            + "/artifacts/"
            + parse.quote(user_seg)
            + "/"
            + parse.quote(safe_name)
        )
        return url

    except Exception as e:
        logger.error(
            "Error uploading to S3 artifacts: %s", type(e).__name__, exc_info=True
        )
        return None
