#!/usr/bin/env python3
"""
AWS Infrastructure Installer using boto3
This script creates AWS infrastructure resources equivalent to the CDK stack.
"""

import boto3
import json
import time
import logging
import argparse
import base64
import ipaddress
import re
import getpass
import secrets
import subprocess
import shutil
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from botocore.exceptions import ClientError, NoCredentialsError, ParamValidationError
import urllib.request
import urllib.error

try:
    from bedrock_agentcore.memory import MemoryClient
except ImportError:
    MemoryClient = None  # type: ignore

# Configuration
project_name = "strands-runtime" # at least 3 characters
region = "us-west-2"
AGENTCORE_GATEWAY_REGION = "us-east-1"
AGENTCORE_WEBSEARCH_GATEWAY_NAME = "gateway-websearch"
AGENTCORE_WEBSEARCH_TARGET_NAME = "websearch"
# Web-search gateway targets use targetConfiguration.mcp.connector (added in boto3 1.43.32).
MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR = "1.43.32"
git_name = "strands-runtime"
# SSE streaming: long tool runs can go 30s+ without tokens; CloudFront/ALB must stay open.
# CloudFront OriginReadTimeout account max is typically 60–120s (ALB idle can be higher).
SSE_ORIGIN_READ_TIMEOUT_SECONDS = 60
ALB_IDLE_TIMEOUT_SECONDS = 600



def agent_runtime_name(project_name: str) -> str:
    """Return Bedrock AgentCore runtime name (e.g. strands_runtime)."""
    return project_name.replace("-", "_")


sts_client = boto3.client("sts", region_name=region)
account_id = sts_client.get_caller_identity()["Account"]

vector_index_name = project_name
vector_bucket_name = f"{project_name}-{account_id}"
embedding_dimensions = 1024
embedding_data_type = "float32"
distance_metric = "cosine"
custom_header_name = "X-Custom-Header"
# Origin header secret value lives in Secrets Manager (never hardcode in source).
ALB_ORIGIN_HEADER_SECRET_NAME = f"{project_name}/cloudfront-alb-origin-header"
SESSION_SIGNING_KEY_SECRET_NAME = f"{project_name}/session-signing-key"
SCHEDULE_AGENT_TOKEN_SECRET_NAME = f"{project_name}/schedule-agent-token"
SCHEDULE_JOBS_TABLE_NAME = f"dynamodb-{project_name}-schedules"
SCHEDULE_LAMBDA_NAME = f"lambda-schedule-for-{project_name}"
SCHEDULE_LAMBDA_ROLE_NAME = f"role-lambda-schedule-for-{project_name}-{region}"
SCHEDULER_INVOKE_ROLE_NAME = f"role-scheduler-invoke-for-{project_name}-{region}"
SCHEDULE_GROUP_NAME = f"schedule-group-{project_name}"
CLOUDFRONT_SIGNING_KEY_SECRET_NAME = f"{project_name}/cloudfront-signing-key"
CLOUDFRONT_S3_SIGNED_PATHS = ("/images/*", "/docs/*", "/artifacts/*")
# Prefer a project custom ResponseHeadersPolicy (security headers + strip origin Server).
# Managed SecurityHeadersPolicy (67f7725c-…) does not remove Server: uvicorn.
_cloudfront_response_headers_policy_id: Optional[str] = None


def _s3_prefixes_for_cloudfront() -> List[str]:
    """Map CloudFront path patterns (/images/*) to S3 key prefixes (images)."""
    prefixes: List[str] = []
    for pattern in CLOUDFRONT_S3_SIGNED_PATHS:
        trimmed = pattern.strip("/")
        if trimmed.endswith("/*"):
            trimmed = trimmed[:-2]
        elif trimmed.endswith("*"):
            trimmed = trimmed[:-1].rstrip("/")
        if trimmed:
            prefixes.append(trimmed)
    return prefixes


def _cloudfront_oai_get_object_resources(s3_bucket_name: str) -> List[str]:
    """S3 object ARNs CloudFront OAI may read (matches CF S3 cache behaviors)."""
    return [
        f"arn:aws:s3:::{s3_bucket_name}/{prefix}/*"
        for prefix in _s3_prefixes_for_cloudfront()
    ]


def _find_s3_oai_id_from_distribution(dist_id: str) -> Optional[str]:
    """Return CloudFront OAI id attached to this distribution's S3 origin, if any."""
    try:
        dist = cloudfront_client.get_distribution_config(Id=dist_id)["DistributionConfig"]
    except ClientError:
        return None
    for origin in (dist.get("Origins") or {}).get("Items") or []:
        oai_path = (
            (origin.get("S3OriginConfig") or {}).get("OriginAccessIdentity") or ""
        ).strip()
        if oai_path:
            return oai_path.rsplit("/", 1)[-1]
    return None


def ensure_cloudfront_oai_bucket_policy(s3_bucket_name: str, oai_id: str) -> None:
    """Restrict OAI s3:GetObject to CF-served prefixes (images/docs/artifacts)."""
    if not oai_id:
        raise ValueError("oai_id is required for CloudFront S3 bucket policy")
    resources = _cloudfront_oai_get_object_resources(s3_bucket_name)
    if not resources:
        raise ValueError("No CloudFront S3 prefixes configured")

    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFrontAccess",
                "Effect": "Allow",
                "Principal": {
                    "AWS": (
                        f"arn:aws:iam::cloudfront:user/"
                        f"CloudFront Origin Access Identity {oai_id}"
                    )
                },
                "Action": "s3:GetObject",
                "Resource": resources,
            }
        ],
    }
    s3_client.put_bucket_policy(
        Bucket=s3_bucket_name,
        Policy=json.dumps(bucket_policy),
    )
    logger.info(
        "  ✓ S3 bucket policy: OAI GetObject limited to %s",
        ", ".join(_s3_prefixes_for_cloudfront()),
    )



# Bedrock Knowledge Base requires these metadata keys as non-filterable on S3 Vectors index
BEDROCK_NON_FILTERABLE_METADATA_KEYS = [
    "AMAZON_BEDROCK_TEXT",
    "AMAZON_BEDROCK_METADATA",
]

# Initialize boto3 clients
s3_client = boto3.client("s3", region_name=region)
iam_client = boto3.client("iam", region_name=region)
opensearch_client = boto3.client("opensearchserverless", region_name=region)
s3vectors_client = boto3.client("s3vectors", region_name=region)
ec2_client = boto3.client("ec2", region_name=region)
elbv2_client = boto3.client("elbv2", region_name=region)
cloudfront_client = boto3.client("cloudfront", region_name=region)
lambda_client = boto3.client("lambda", region_name=region)
dynamodb_client = boto3.client("dynamodb", region_name=region)
scheduler_client = boto3.client("scheduler", region_name=region)
ssm_client = boto3.client("ssm", region_name=region)
secretsmanager_client = boto3.client("secretsmanager", region_name=region)
ecr_client = boto3.client("ecr", region_name=region)
ecs_client = boto3.client("ecs", region_name=region)
logs_client = boto3.client("logs", region_name=region)
agentcore_control_client = boto3.client(
    "bedrock-agentcore-control",
    region_name=AGENTCORE_GATEWAY_REGION,
)
s3files_client = boto3.client("s3files", region_name=region)
cognito_idp_client = boto3.client("cognito-idp", region_name=region)

S3_FILES_SESSION_PREFIX = "agentcore-sessions/"
S3_FILES_APP_DATA_PREFIX = "app-data/"
APP_DATA_MOUNT_PATH = "/mnt/app-data"
SESSION_STORAGE_MOUNT_PATH = "/mnt/workspace"
COGNITO_ADMIN_USERNAME = "admin"
COGNITO_CLIENT_NAME = f"{project_name}-web-ui"

bucket_name = f"storage-for-{project_name}-{account_id}-{region}"


def s3_vectors_bucket_arn(bucket_name: str = vector_bucket_name) -> str:
    """ARN for an S3 vector bucket."""
    return f"arn:aws:s3vectors:{region}:{account_id}:bucket/{bucket_name}"


def s3_vectors_index_arn(
    index_name: str = vector_index_name,
    bucket_name: str = vector_bucket_name,
) -> str:
    """ARN for a vector index within an S3 vector bucket."""
    return f"{s3_vectors_bucket_arn(bucket_name)}/index/{index_name}"


# Configure logging
def setup_logging(log_level=logging.INFO):
    """Setup logging configuration."""
    log_format = "%(asctime)s - %(levelname)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(),
            # logging.FileHandler(f"installer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
        ]
    )
    
    return logging.getLogger(__name__)


logger = setup_logging()


def _generate_alb_origin_header_value() -> str:
    """Cryptographically random origin-verification header value."""
    return secrets.token_urlsafe(32)


def get_or_create_alb_origin_header(*, rotate: bool = False) -> str:
    """
    Return CloudFront→ALB origin header from Secrets Manager.

    Generates a cryptographically random value on first create (never hardcoded
    in source). Reuses the stored value on subsequent installs unless rotate=True.
    """
    secret_name = ALB_ORIGIN_HEADER_SECRET_NAME

    try:
        existing = secretsmanager_client.get_secret_value(SecretId=secret_name)
        current = (existing.get("SecretString") or "").strip()
        if current and not rotate:
            logger.info(f"  ✓ Reusing ALB origin header from Secrets Manager: {secret_name}")
            return current
        new_value = _generate_alb_origin_header_value()
        secretsmanager_client.put_secret_value(
            SecretId=secret_name,
            SecretString=new_value,
        )
        logger.info(f"  ✓ Rotated ALB origin header in Secrets Manager: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    new_value = _generate_alb_origin_header_value()
    try:
        secretsmanager_client.create_secret(
            Name=secret_name,
            Description=(
                f"CloudFront to ALB origin verification header ({custom_header_name}) "
                f"for {project_name}"
            ),
            SecretString=new_value,
            Tags=[
                {"Key": "Name", "Value": secret_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        logger.info(f"  ✓ Created ALB origin header secret: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            if rotate:
                new_value = _generate_alb_origin_header_value()
                secretsmanager_client.put_secret_value(
                    SecretId=secret_name,
                    SecretString=new_value,
                )
                logger.info(f"  ✓ Rotated ALB origin header in Secrets Manager: {secret_name}")
                return new_value
            response = secretsmanager_client.get_secret_value(SecretId=secret_name)
            return response["SecretString"]
        raise


def _describe_secret_arn(secret_id: str) -> str:
    """Return the full Secrets Manager ARN (includes random suffix)."""
    return secretsmanager_client.describe_secret(SecretId=secret_id)["ARN"]


def _ecs_secret_value_from(secret_name: str, *, json_key: Optional[str] = None) -> str:
    """Build ECS containerSecrets valueFrom (ARN or ARN:json_key::)."""
    arn = _describe_secret_arn(secret_name)
    if json_key:
        return f"{arn}:{json_key}::"
    return arn


def get_or_create_session_signing_key(*, rotate: bool = False) -> str:
    """
    Ensure HMAC key for Web UI session cookies exists in Secrets Manager.

    ECS injects the value via task-definition ``secrets`` (ARN), never as
    plaintext ``environment``. Callers should not put the return value into
    the task definition.
    """
    secret_name = SESSION_SIGNING_KEY_SECRET_NAME

    try:
        existing = secretsmanager_client.get_secret_value(SecretId=secret_name)
        current = (existing.get("SecretString") or "").strip()
        if current and not rotate:
            logger.info(f"  ✓ Reusing session signing key from Secrets Manager: {secret_name}")
            return current
        new_value = secrets.token_urlsafe(32)
        secretsmanager_client.put_secret_value(
            SecretId=secret_name,
            SecretString=new_value,
        )
        logger.info(f"  ✓ Rotated session signing key in Secrets Manager: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    new_value = secrets.token_urlsafe(32)
    try:
        secretsmanager_client.create_secret(
            Name=secret_name,
            Description=f"HMAC signing key for {project_name} Web UI session cookies",
            SecretString=new_value,
            Tags=[
                {"Key": "Name", "Value": secret_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        logger.info(f"  ✓ Created session signing key secret: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            if rotate:
                new_value = secrets.token_urlsafe(32)
                secretsmanager_client.put_secret_value(
                    SecretId=secret_name,
                    SecretString=new_value,
                )
                logger.info(f"  ✓ Rotated session signing key in Secrets Manager: {secret_name}")
                return new_value
            response = secretsmanager_client.get_secret_value(SecretId=secret_name)
            return response["SecretString"]
        raise


def _ecs_execution_secrets_policy_document() -> Dict:
    """Allow ECS task execution role to inject project secrets into containers."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadProjectSecretsForEcsInjection",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": [
                    f"arn:aws:secretsmanager:{region}:{account_id}:secret:{project_name}/session-signing-key*",
                    f"arn:aws:secretsmanager:{region}:{account_id}:secret:{project_name}/schedule-agent-token*",
                    f"arn:aws:secretsmanager:{region}:{account_id}:secret:{project_name}/cloudfront-signing-key*",
                ],
            }
        ],
    }


def attach_ecs_execution_secrets_policy(execution_role_name: str) -> None:
    attach_inline_policy(
        execution_role_name,
        f"ecs-execution-secrets-for-{project_name}",
        _ecs_execution_secrets_policy_document(),
    )
    logger.info(f"  ✓ ECS execution role can read project secrets: {execution_role_name}")


def _alb_forbidden_default_action() -> Dict:
    """ALB default action: deny requests missing the origin header."""
    return {
        "Type": "fixed-response",
        "FixedResponseConfig": {
            "StatusCode": "403",
            "ContentType": "text/plain",
            "MessageBody": "Forbidden",
        },
    }


def _alb_default_action_is_forbidden(actions: Optional[List[Dict]]) -> bool:
    if not actions:
        return False
    action = actions[0]
    if action.get("Type") != "fixed-response":
        return False
    status = str(action.get("FixedResponseConfig", {}).get("StatusCode", ""))
    return status == "403"


def _cloudfront_alb_origin_custom_headers(header_value: str) -> Dict[str, object]:
    return {
        "Quantity": 1,
        "Items": [
            {
                "HeaderName": custom_header_name,
                "HeaderValue": header_value,
            }
        ],
    }


def ensure_alb_listener_origin_protection(
    alb_arn: str,
    tg_arn: str,
    header_value: str,
) -> str:
    """
    Ensure HTTP:80 listener denies by default and forwards only when the
    CloudFront origin header matches.
    """
    listener_arn = None
    existing_listener = None
    try:
        listeners = elbv2_client.describe_listeners(LoadBalancerArn=alb_arn)
        for listener in listeners.get("Listeners", []):
            if listener["Port"] == 80 and listener["Protocol"] == "HTTP":
                listener_arn = listener["ListenerArn"]
                existing_listener = listener
                logger.warning(f"  Listener already exists on port 80: {listener_arn}")
                break
    except ClientError as e:
        logger.warning(f"  Error checking existing listeners: {e}")

    if not listener_arn:
        listener_response = elbv2_client.create_listener(
            LoadBalancerArn=alb_arn,
            Protocol="HTTP",
            Port=80,
            DefaultActions=[_alb_forbidden_default_action()],
        )
        listener_arn = listener_response["Listeners"][0]["ListenerArn"]
        logger.info(f"  ✓ Created ALB listener (default 403): {listener_arn}")
    elif not _alb_default_action_is_forbidden(existing_listener.get("DefaultActions")):
        elbv2_client.modify_listener(
            ListenerArn=listener_arn,
            DefaultActions=[_alb_forbidden_default_action()],
        )
        logger.info("  ✓ Updated ALB listener default action to 403 fixed-response")

    header_rule_arn = None
    header_rule_matches = False
    try:
        rules = elbv2_client.describe_rules(ListenerArn=listener_arn)
        for rule in rules.get("Rules", []):
            if rule.get("Priority") == "default":
                continue
            for condition in rule.get("Conditions", []):
                if condition.get("Field") != "http-header":
                    continue
                header_cfg = condition.get("HttpHeaderConfig") or {}
                if header_cfg.get("HttpHeaderName") != custom_header_name:
                    continue
                header_rule_arn = rule["RuleArn"]
                values = header_cfg.get("Values") or []
                header_rule_matches = header_value in values
                break
            if header_rule_arn:
                break
    except ClientError as e:
        logger.debug(f"  Error checking existing rules: {e}")

    forward_action = [{"Type": "forward", "TargetGroupArn": tg_arn}]
    header_condition = [
        {
            "Field": "http-header",
            "HttpHeaderConfig": {
                "HttpHeaderName": custom_header_name,
                "Values": [header_value],
            },
        }
    ]

    if header_rule_arn:
        elbv2_client.modify_rule(
            RuleArn=header_rule_arn,
            Conditions=header_condition,
            Actions=forward_action,
        )
        if header_rule_matches:
            logger.info("  ✓ ALB custom-header forward rule is up to date")
        else:
            logger.info("  ✓ Updated ALB custom-header forward rule with secret value")
    else:
        try:
            elbv2_client.create_rule(
                ListenerArn=listener_arn,
                Priority=10,
                Conditions=header_condition,
                Actions=forward_action,
            )
            logger.info("  ✓ Created ALB rule: custom header -> forward")
        except ClientError as e:
            if e.response["Error"]["Code"] in ["PriorityInUse", "RuleAlreadyExists"]:
                # Priority 10 may be an unrelated rule; replace its conditions.
                rules = elbv2_client.describe_rules(ListenerArn=listener_arn)
                for rule in rules.get("Rules", []):
                    if rule.get("Priority") == "10":
                        elbv2_client.modify_rule(
                            RuleArn=rule["RuleArn"],
                            Conditions=header_condition,
                            Actions=forward_action,
                        )
                        logger.info(
                            "  ✓ Replaced priority-10 rule with custom-header forward"
                        )
                        break
                else:
                    raise
            else:
                raise

    return listener_arn


def create_s3_bucket() -> str:
    """Create S3 bucket with CORS configuration."""
    logger.info(f"[1/10] Creating S3 bucket: {bucket_name}")
    
    try:
        # Create bucket
        logger.debug(f"Creating bucket in region: {region}")
        if region == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )
        logger.debug("Bucket created successfully")
        
        # Configure bucket
        logger.debug("Configuring public access block")
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True
            }
        )
        
        # Set CORS configuration
        logger.debug("Setting CORS configuration")
        cors_configuration = {
            "CORSRules": [
                {
                    "AllowedHeaders": ["*"],
                    "AllowedMethods": ["GET", "POST", "PUT"],
                    "AllowedOrigins": ["*"]
                }
            ]
        }
        s3_client.put_bucket_cors(
            Bucket=bucket_name,
            CORSConfiguration=cors_configuration
        )
        
        # S3 Files requires bucket versioning to be Enabled.
        logger.debug("Enabling bucket versioning")
        s3_client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={"Status": "Enabled"}
        )
        
        # Create docs folder
        logger.debug("Creating docs folder")
        try:
            s3_client.put_object(
                Bucket=bucket_name,
                Key="docs/",
                Body=b""
            )
            logger.debug("docs folder created successfully")
        except ClientError as e:
            logger.warning(f"Failed to create docs folder: {e}")
        
        logger.info(f"✓ S3 bucket created successfully: {bucket_name}")
        return bucket_name
    
    except ClientError as e:
        if e.response["Error"]["Code"] in ["BucketAlreadyExists", "BucketAlreadyOwnedByYou"]:
            logger.warning(f"S3 bucket already exists: {bucket_name}")
            # Create docs folder if bucket already exists
            logger.debug("Creating docs folder in existing bucket")
            try:
                s3_client.put_object(
                    Bucket=bucket_name,
                    Key="docs/",
                    Body=b""
                )
                logger.debug("docs folder created successfully")
            except ClientError as folder_error:
                if folder_error.response["Error"]["Code"] != "NoSuchBucket":
                    logger.warning(f"Failed to create docs folder: {folder_error}")
            return bucket_name
        logger.error(f"Failed to create S3 bucket: {e}")
        raise


def create_iam_role(role_name: str, assume_role_policy: Dict, managed_policies: Optional[List[str]] = None) -> str:
    """Create IAM role."""
    logger.debug(f"Creating IAM role: {role_name}")
    
    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description=f"Role for {role_name}"
        )
        role_arn = response["Role"]["Arn"]
        logger.debug(f"Role created: {role_arn}")
        
        if managed_policies:
            logger.debug(f"Attaching {len(managed_policies)} managed policies")
            for policy_arn in managed_policies:
                iam_client.attach_role_policy(
                    RoleName=role_name,
                    PolicyArn=policy_arn
                )
                logger.debug(f"Attached policy: {policy_arn}")
        
        logger.info(f"✓ IAM role created: {role_name}")
        return role_arn
    
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.warning(f"IAM role already exists: {role_name}")
            response = iam_client.get_role(RoleName=role_name)
            role_arn = response["Role"]["Arn"]
            
            # Update trust policy for existing role
            try:
                logger.info(f"Updating trust policy for existing role: {role_name}")
                iam_client.update_assume_role_policy(
                    RoleName=role_name,
                    PolicyDocument=json.dumps(assume_role_policy)
                )
                logger.info(f"✓ Updated trust policy for role: {role_name}")
                
                # Verify trust policy was updated correctly
                updated_role = iam_client.get_role(RoleName=role_name)
                policy_doc = updated_role["Role"]["AssumeRolePolicyDocument"]
                # Handle both string and dict formats (boto3 may return either)
                if isinstance(policy_doc, str):
                    updated_policy = json.loads(policy_doc)
                else:
                    updated_policy = policy_doc
                logger.debug(f"Verified trust policy: {json.dumps(updated_policy, indent=2)}")
            except ClientError as trust_policy_error:
                logger.error(f"✗ Failed to update trust policy for role {role_name}: {trust_policy_error}")
                logger.error(f"  Error Code: {trust_policy_error.response.get('Error', {}).get('Code')}")
                logger.error(f"  Error Message: {trust_policy_error.response.get('Error', {}).get('Message')}")
                raise
            
            # Update managed policies if provided
            if managed_policies:
                logger.debug(f"Updating managed policies for existing role")
                # Get currently attached managed policies
                try:
                    attached_policies = iam_client.list_attached_role_policies(RoleName=role_name)
                    current_policy_arns = {policy["PolicyArn"] for policy in attached_policies["AttachedPolicies"]}
                    
                    # Attach missing policies
                    for policy_arn in managed_policies:
                        if policy_arn not in current_policy_arns:
                            iam_client.attach_role_policy(
                                RoleName=role_name,
                                PolicyArn=policy_arn
                            )
                            logger.debug(f"Attached missing policy: {policy_arn}")
                except ClientError as policy_error:
                    logger.warning(f"Could not update managed policies: {policy_error}")
            
            return role_arn
        logger.error(f"Failed to create IAM role {role_name}: {e}")
        raise


def attach_inline_policy(role_name: str, policy_name: str, policy_document: Dict):
    """Attach or update inline policy to IAM role."""
    logger.debug(f"Attaching/updating inline policy {policy_name} to {role_name}")
    
    try:
        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document)
        )
        logger.debug(f"Policy {policy_name} attached/updated successfully")
    except ClientError as e:
        logger.error(f"Error attaching/updating policy {policy_name}: {e}")
        raise



def _bedrock_knowledge_base_trust_policy() -> Dict:
    """Trust policy for Bedrock Knowledge Base service role (AWS recommended)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "bedrock.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"
                        )
                    },
                },
            }
        ],
    }


def _principal_allows_service(principal: Dict, service: str) -> bool:
    """Return True when principal allows the given AWS service."""
    if not isinstance(principal, dict):
        return False
    allowed = principal.get("Service", [])
    if isinstance(allowed, str):
        allowed = [allowed]
    return service in allowed


def wait_for_iam_role_propagation(role_name: str, wait_seconds: int = 15) -> None:
    """Wait for IAM role and inline policies to propagate."""
    logger.info(f"  Waiting {wait_seconds}s for IAM role propagation: {role_name}")
    time.sleep(wait_seconds)

    expected_policies = {
        f"bedrock-invoke-policy-for-{project_name}",
        f"knowledge-base-s3-policy-for-{project_name}",
        f"bedrock-agent-s3vectors-policy-for-{project_name}",
        f"bedrock-agent-bedrock-policy-for-{project_name}",
    }
    for attempt in range(3):
        try:
            attached = iam_client.list_role_policies(RoleName=role_name)
            missing = expected_policies - set(attached.get("PolicyNames", []))
            if not missing:
                logger.info("  ✓ Knowledge Base role inline policies are attached")
                return
            logger.debug(
                f"  Waiting for inline policies (attempt {attempt + 1}/3): {sorted(missing)}"
            )
        except ClientError as e:
            logger.debug(f"  Could not list role policies yet: {e}")
        time.sleep(5)

    logger.warning(
        "  Some Knowledge Base role inline policies may not be visible yet; continuing"
    )


def _project_s3_bucket_arns() -> Tuple[str, str]:
    """Return (bucket ARN, object ARN) for the project storage bucket."""
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    return bucket_arn, f"{bucket_arn}/*"


def create_knowledge_base_role() -> str:
    """Create Knowledge Base IAM role with least-privilege policies."""
    logger.info("[2/10] Creating Knowledge Base IAM role")
    role_name = f"role-knowledge-base-for-{project_name}-{region}"
    
    assume_role_policy = _bedrock_knowledge_base_trust_policy()
    
    role_existed = False
    try:
        iam_client.get_role(RoleName=role_name)
        role_existed = True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in (
            "NoSuchEntity",
            "NoSuchEntityException",
        ):
            raise

    role_arn = create_iam_role(role_name, assume_role_policy)
    bucket_arn, object_arn = _project_s3_bucket_arns()

    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeEmbeddingModels",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                    f"arn:aws:bedrock:{region}:*:inference-profile/*",
                ],
            }
        ],
    }
    attach_inline_policy(role_name, f"kb-bedrock-policy-for-{project_name}", bedrock_policy)

    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListKnowledgeBaseBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [bucket_arn],
            },
            {
                "Sid": "ReadKnowledgeBaseObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [object_arn],
            },
        ],
    }
    attach_inline_policy(role_name, f"kb-s3-policy-for-{project_name}", s3_policy)

    opensearch_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "OpenSearchServerlessAccess",
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": [f"arn:aws:aoss:{region}:{account_id}:collection/*"],
            }
        ],
    }
    attach_inline_policy(role_name, f"kb-opensearch-policy-for-{project_name}", opensearch_policy)
    
    if not role_existed:
        wait_for_iam_role_propagation(role_name)
    else:
        logger.info(
            f"  Skipping IAM wait (KB role already exists: {role_name})"
        )
    return role_arn


def _agent_runtime_arns_for_name(runtime_name: str) -> List[str]:
    """IAM Resource ARNs for one AgentCore runtime name (+ endpoints)."""
    return [
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_name}",
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_name}-*",
        (
            f"arn:aws:bedrock-agentcore:{region}:{account_id}:"
            f"runtime/{runtime_name}/runtime-endpoint/*"
        ),
        (
            f"arn:aws:bedrock-agentcore:{region}:{account_id}:"
            f"runtime/{runtime_name}-*/runtime-endpoint/*"
        ),
    ]


def _read_local_json_config(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _ecs_agent_runtime_resource_arns() -> List[str]:
    """
    AgentCore runtime ARNs allowed for the ECS Web UI task role.

    Scans application/config.json and every runtime_agent/*/config.json so
    project_name, folder names (e.g. strands), and explicit agent_runtime_arn
    values are all covered.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    app_config = _read_local_json_config(os.path.join(root, "application", "config.json"))
    runtime_configs = [app_config]
    runtime_agent_dir = os.path.join(root, "runtime_agent")
    if os.path.isdir(runtime_agent_dir):
        for entry in sorted(os.listdir(runtime_agent_dir)):
            cfg_path = os.path.join(runtime_agent_dir, entry, "config.json")
            if os.path.isfile(cfg_path):
                runtime_configs.append(_read_local_json_config(cfg_path))

    name_candidates = {
        agent_runtime_name(project_name),
        agent_runtime_name(git_name),
        # Common default / legacy name used by runtime_agent/strands/config.json
        agent_runtime_name("strands-runtime"),
    }
    if os.path.isdir(runtime_agent_dir):
        for entry in os.listdir(runtime_agent_dir):
            entry_path = os.path.join(runtime_agent_dir, entry)
            if os.path.isdir(entry_path) and not entry.startswith("."):
                name_candidates.add(agent_runtime_name(entry))
    for config in runtime_configs:
        runtime_project = (config.get("projectName") or "").strip()
        if runtime_project:
            name_candidates.add(agent_runtime_name(runtime_project))

    arns: List[str] = []
    seen = set()
    for name in sorted(n for n in name_candidates if n):
        for arn in _agent_runtime_arns_for_name(name):
            if arn not in seen:
                seen.add(arn)
                arns.append(arn)

    for config in runtime_configs:
        agent_runtime_arn = (config.get("agent_runtime_arn") or "").strip()
        if not agent_runtime_arn:
            continue
        if agent_runtime_arn not in seen:
            seen.add(agent_runtime_arn)
            arns.append(agent_runtime_arn)
        endpoint_arn = f"{agent_runtime_arn}/runtime-endpoint/*"
        if endpoint_arn not in seen:
            seen.add(endpoint_arn)
            arns.append(endpoint_arn)

    return arns


def _get_ecs_task_inline_policies() -> List[Dict]:
    """Least-privilege inline IAM policies for the ECS task (Web UI) role."""
    bucket_arn, object_arn = _project_s3_bucket_arns()
    runtime_resource_arns = _ecs_agent_runtime_resource_arns()
    return [
        {
            "name": f"ecs-task-bedrock-policy-for-{project_name}",
            "document": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "InvokeBedrockModels",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock:InvokeModel",
                            "bedrock:InvokeModelWithResponseStream",
                            "bedrock:ApplyGuardrail",
                            "bedrock:GetInferenceProfile",
                            "bedrock:GetFoundationModel",
                        ],
                        "Resource": [
                            "arn:aws:bedrock:*::foundation-model/*",
                            f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                            f"arn:aws:bedrock:*:{account_id}:inference-profile/*",
                            f"arn:aws:bedrock:{region}:{account_id}:guardrail/*",
                            f"arn:aws:bedrock:{region}:{account_id}:guardrail-profile/*",
                        ],
                    },
                    {
                        "Sid": "KnowledgeBaseIngestion",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock:StartIngestionJob",
                            "bedrock:ListIngestionJobs",
                            "bedrock:GetIngestionJob",
                        ],
                        "Resource": [
                            f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*",
                        ],
                    },
                    {
                        "Sid": "BedrockMantleOpenAI",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-mantle:Get*",
                            "bedrock-mantle:List*",
                            "bedrock-mantle:CreateInference",
                        ],
                        "Resource": [
                            f"arn:aws:bedrock-mantle:*:{account_id}:project/*",
                        ],
                    },
                    {
                        "Sid": "BedrockMantleBearerToken",
                        "Effect": "Allow",
                        "Action": ["bedrock-mantle:CallWithBearerToken"],
                        "Resource": ["*"],
                    },
                ],
            },
        },
        {
            "name": f"ecs-task-agentcore-policy-for-{project_name}",
            "document": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "InvokeAndGetAgentRuntime",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:InvokeAgentRuntime",
                            "bedrock-agentcore:InvokeAgentRuntimeWithWebResponse",
                            "bedrock-agentcore:GetAgentRuntime",
                            "bedrock-agentcore-control:GetAgentRuntime",
                        ],
                        # Root project_name and Agent Runtime installer naming can differ
                        # (e.g. strands-runtime vs strands). See _ecs_agent_runtime_resource_arns.
                        "Resource": runtime_resource_arns,
                    },
                    {
                        "Sid": "ListAgentRuntimes",
                        "Effect": "Allow",
                        "Action": [
                            "bedrock-agentcore:ListAgentRuntimes",
                            "bedrock-agentcore-control:ListAgentRuntimes",
                        ],
                        "Resource": ["*"],
                    },
                ],
            },
        },
        {
            "name": f"ecs-task-s3-policy-for-{project_name}",
            "document": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "ListProjectBucket",
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                        "Resource": [bucket_arn],
                    },
                    {
                        "Sid": "ReadWriteProjectObjects",
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                        ],
                        "Resource": [object_arn],
                    },
                ],
            },
        },
        {
            "name": f"ecs-task-cognito-policy-for-{project_name}",
            "document": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "CognitoUserPasswordAuth",
                        "Effect": "Allow",
                        "Action": [
                            "cognito-idp:InitiateAuth",
                            "cognito-idp:RespondToAuthChallenge",
                            "cognito-idp:GetUser",
                            "cognito-idp:DescribeUserPool",
                            "cognito-idp:DescribeUserPoolClient",
                        ],
                        "Resource": ["*"],
                    },
                ],
            },
        },
    ]


def create_ecs_roles() -> Dict[str, str]:
    """Create ECS task role and task execution role."""
    logger.info("[2/10] Creating ECS IAM roles")

    task_role_name = f"role-ecs-task-for-{project_name}-{region}"
    execution_role_name = f"role-ecs-execution-for-{project_name}-{region}"

    task_assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    }
    execution_assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    }

    task_role_arn = create_iam_role(task_role_name, task_assume_role_policy)
    execution_role_arn = create_iam_role(
        execution_role_name,
        execution_assume_role_policy,
        managed_policies=["arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"],
    )

    for policy in _get_ecs_task_inline_policies():
        attach_inline_policy(task_role_name, policy["name"], policy["document"])

    attach_ecs_execution_secrets_policy(execution_role_name)

    return {
        "task_role_arn": task_role_arn,
        "execution_role_arn": execution_role_arn,
    }


def _find_cognito_user_pool_id(pool_name: str) -> Optional[str]:
    """Return User Pool ID if a pool with the given name already exists."""
    next_token = None
    while True:
        kwargs: Dict = {"MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp_client.list_user_pools(**kwargs)
        for pool in response.get("UserPools", []):
            if pool.get("Name") == pool_name:
                return pool["Id"]
        next_token = response.get("NextToken")
        if not next_token:
            return None


def _cognito_pool_id_from_config() -> Optional[str]:
    """Return cognito_user_pool_id from application/config.json if present."""
    try:
        with open(_application_config_path(), "r", encoding="utf-8") as f:
            config = json.load(f)
        pool_id = (config.get("cognito_user_pool_id") or "").strip()
        return pool_id or None
    except (OSError, json.JSONDecodeError, TypeError, NameError):
        return None


def _cognito_user_pool_exists(user_pool_id: str) -> bool:
    try:
        cognito_idp_client.describe_user_pool(UserPoolId=user_pool_id)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException", "UserPoolNotFoundException"):
            return False
        raise


def _resolve_cognito_user_pool_id(pool_name: Optional[str] = None) -> Optional[str]:
    """Prefer a live config.json pool id, then list User Pools by project name."""
    pool_id = _cognito_pool_id_from_config()
    if pool_id and _cognito_user_pool_exists(pool_id):
        return pool_id
    return _find_cognito_user_pool_id(pool_name or project_name)


def _find_cognito_client_id(user_pool_id: str, client_name: str) -> Optional[str]:
    """Return App Client ID if a client with the given name already exists."""
    next_token = None
    while True:
        kwargs: Dict = {"UserPoolId": user_pool_id, "MaxResults": 60}
        if next_token:
            kwargs["NextToken"] = next_token
        response = cognito_idp_client.list_user_pool_clients(**kwargs)
        for client in response.get("UserPoolClients", []):
            if client.get("ClientName") == client_name:
                return client["ClientId"]
        next_token = response.get("NextToken")
        if not next_token:
            return None


def _cognito_password_valid(password: str) -> Optional[str]:
    """Return an error message if password does not meet Cognito policy, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return "Password must include at least one uppercase letter"
    if not any(c.islower() for c in password):
        return "Password must include at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return "Password must include at least one number"
    return None


def prompt_cognito_admin_password() -> str:
    """Prompt interactively for the Cognito admin password (confirmed twice)."""
    logger.info("")
    logger.info("Cognito admin user registration")
    logger.info(f"  Username: {COGNITO_ADMIN_USERNAME}")
    logger.info(
        "  Password policy: min 8 chars, uppercase, lowercase, number "
        "(symbols optional)"
    )
    while True:
        password = getpass.getpass(
            f"Enter password for Cognito admin '{COGNITO_ADMIN_USERNAME}': "
        )
        error = _cognito_password_valid(password)
        if error:
            logger.warning(f"  {error}. Try again.")
            continue
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            logger.warning("  Passwords do not match. Try again.")
            continue
        return password


def _cognito_admin_exists(user_pool_id: str, username: str) -> bool:
    try:
        cognito_idp_client.admin_get_user(UserPoolId=user_pool_id, Username=username)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("UserNotFoundException", "ResourceNotFoundException"):
            return False
        raise


def _create_cognito_admin_user(user_pool_id: str, username: str, password: str) -> None:
    cognito_idp_client.admin_create_user(
        UserPoolId=user_pool_id,
        Username=username,
        TemporaryPassword=password,
        MessageAction="SUPPRESS",
    )
    cognito_idp_client.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def create_cognito_user_pool() -> Dict[str, str]:
    """Create Cognito User Pool (named project_name), app client, and admin user.

    Admin password is prompted via getpass when the admin user does not yet exist.
    """
    logger.info("Creating Cognito User Pool for Web UI authentication")
    pool_name = project_name
    user_pool_id = _resolve_cognito_user_pool_id(pool_name)

    if user_pool_id:
        logger.info(f"  ✓ Reusing Cognito User Pool: {user_pool_id} (name={pool_name})")
    else:
        response = cognito_idp_client.create_user_pool(
            PoolName=pool_name,
            Policies={
                "PasswordPolicy": {
                    "MinimumLength": 8,
                    "RequireUppercase": True,
                    "RequireLowercase": True,
                    "RequireNumbers": True,
                    "RequireSymbols": False,
                }
            },
            MfaConfiguration="OFF",
            AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
            Schema=[
                {
                    "Name": "email",
                    "AttributeDataType": "String",
                    "Mutable": True,
                    "Required": False,
                }
            ],
        )
        user_pool_id = response["UserPool"]["Id"]
        logger.info(f"  ✓ Cognito User Pool created: {user_pool_id} (name={pool_name})")

    client_id = _find_cognito_client_id(user_pool_id, COGNITO_CLIENT_NAME)
    if client_id:
        logger.info(f"  ✓ Reusing Cognito App Client: {client_id}")
    else:
        client_response = cognito_idp_client.create_user_pool_client(
            UserPoolId=user_pool_id,
            ClientName=COGNITO_CLIENT_NAME,
            GenerateSecret=False,
            ExplicitAuthFlows=[
                "ALLOW_USER_PASSWORD_AUTH",
                "ALLOW_REFRESH_TOKEN_AUTH",
                "ALLOW_USER_SRP_AUTH",
            ],
            PreventUserExistenceErrors="ENABLED",
        )
        client_id = client_response["UserPoolClient"]["ClientId"]
        logger.info(f"  ✓ Cognito App Client created: {client_id}")

    if _cognito_admin_exists(user_pool_id, COGNITO_ADMIN_USERNAME):
        logger.info(
            f"  ✓ Cognito admin user already exists: {COGNITO_ADMIN_USERNAME} "
            f"— skipping password prompt"
        )
    else:
        password = prompt_cognito_admin_password()
        _create_cognito_admin_user(user_pool_id, COGNITO_ADMIN_USERNAME, password)
        logger.info(f"  ✓ Cognito admin user created: {COGNITO_ADMIN_USERNAME}")

    cognito_info = {
        "cognito_user_pool_id": user_pool_id,
        "cognito_user_pool_name": pool_name,
        "cognito_client_id": client_id,
        "cognito_client_name": COGNITO_CLIENT_NAME,
        "cognito_admin_username": COGNITO_ADMIN_USERNAME,
        "cognito_region": region,
    }
    if write_application_config(cognito_info):
        logger.info("  ✓ Saved Cognito settings to application/config.json")
    return cognito_info



def get_or_create_schedule_agent_token(*, rotate: bool = False) -> str:
    """HMAC token for my-schedule (skill + Lambda → ECS)."""
    secret_name = SCHEDULE_AGENT_TOKEN_SECRET_NAME
    try:
        existing = secretsmanager_client.get_secret_value(SecretId=secret_name)
        current = (existing.get("SecretString") or "").strip()
        if current and not rotate:
            logger.info(f"  ✓ Reusing schedule agent token: {secret_name}")
            return current
        new_value = secrets.token_urlsafe(32)
        secretsmanager_client.put_secret_value(
            SecretId=secret_name,
            SecretString=new_value,
        )
        logger.info(f"  ✓ Rotated schedule agent token: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    new_value = secrets.token_urlsafe(32)
    try:
        secretsmanager_client.create_secret(
            Name=secret_name,
            Description=f"HMAC token for {project_name} my-schedule (skill/Lambda)",
            SecretString=new_value,
            Tags=[
                {"Key": "Name", "Value": secret_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        logger.info(f"  ✓ Created schedule agent token secret: {secret_name}")
        return new_value
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceExistsException":
            if rotate:
                new_value = secrets.token_urlsafe(32)
                secretsmanager_client.put_secret_value(
                    SecretId=secret_name,
                    SecretString=new_value,
                )
                logger.info(f"  ✓ Rotated schedule agent token: {secret_name}")
                return new_value
            response = secretsmanager_client.get_secret_value(SecretId=secret_name)
            return response["SecretString"]
        raise

def create_schedule_jobs_table() -> str:
    """DynamoDB table for my-schedule jobs."""
    table_name = SCHEDULE_JOBS_TABLE_NAME
    logger.info(f"[schedule] Creating DynamoDB table: {table_name}")
    try:
        dynamodb_client.create_table(
            TableName=table_name,
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "task_id", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "user_id-created_at-index",
                    "KeySchema": [
                        {"AttributeName": "user_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "task_id-created_at-index",
                    "KeySchema": [
                        {"AttributeName": "task_id", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
            Tags=[
                {"Key": "Name", "Value": table_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        waiter = dynamodb_client.get_waiter("table_exists")
        waiter.wait(TableName=table_name)
        logger.info(f"  ✓ Created DynamoDB table: {table_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
        logger.warning(f"  DynamoDB table already exists: {table_name}")
    return table_name

def create_schedule_group() -> str:
    group_name = SCHEDULE_GROUP_NAME
    logger.info(f"[schedule] Creating Scheduler group: {group_name}")
    try:
        scheduler_client.create_schedule_group(
            Name=group_name,
            Tags=[
                {"Key": "Name", "Value": group_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        logger.info(f"  ✓ Created schedule group: {group_name}")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in {"ConflictException", "ResourceAlreadyExistsException"}:
            raise
        logger.warning(f"  Schedule group already exists: {group_name}")
    return group_name

def create_scheduler_invoke_role(lambda_arn: str) -> str:
    """EventBridge Scheduler execution role that invokes the schedule Lambda."""
    role_name = SCHEDULER_INVOKE_ROLE_NAME
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                },
            }
        ],
    }
    role_arn = create_iam_role(role_name, trust)
    attach_inline_policy(
        role_name,
        f"scheduler-invoke-lambda-for-{project_name}",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["lambda:InvokeFunction"],
                    "Resource": [lambda_arn, f"{lambda_arn}:*"],
                }
            ],
        },
    )
    return role_arn

def create_schedule_lambda_role() -> str:
    role_name = SCHEDULE_LAMBDA_ROLE_NAME
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    role_arn = create_iam_role(
        role_name,
        trust,
        managed_policies=[
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        ],
    )
    attach_inline_policy(
        role_name,
        f"lambda-schedule-policy-for-{project_name}",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ReadScheduleJobs",
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:Query",
                    ],
                    "Resource": [
                        f"arn:aws:dynamodb:{region}:{account_id}:table/{SCHEDULE_JOBS_TABLE_NAME}",
                        f"arn:aws:dynamodb:{region}:{account_id}:table/{SCHEDULE_JOBS_TABLE_NAME}/index/*",
                    ],
                },
                {
                    "Sid": "ReadScheduleToken",
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": [
                        f"arn:aws:secretsmanager:{region}:{account_id}:secret:{project_name}/schedule-agent-token*",
                    ],
                },
            ],
        },
    )
    return role_arn

def _package_schedule_lambda_zip() -> bytes:
    import io
    import zipfile

    root = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(root, "lambda-schedule", "lambda_function.py")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"Missing schedule lambda source: {src}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(src, arcname="lambda_function.py")
    return buf.getvalue()

def create_or_update_schedule_lambda(
    *,
    role_arn: str,
    app_base_url: str,
    table_name: str,
) -> str:
    """Deploy schedule runner Lambda. Returns function ARN."""
    get_or_create_schedule_agent_token(rotate=False)
    zip_bytes = _package_schedule_lambda_zip()
    fn_name = SCHEDULE_LAMBDA_NAME
    secret_arn = _describe_secret_arn(SCHEDULE_AGENT_TOKEN_SECRET_NAME)
    env = {
        "Variables": {
            "APP_BASE_URL": (app_base_url or "").rstrip("/"),
            "SCHEDULE_JOBS_TABLE": table_name,
            "SCHEDULE_AGENT_TOKEN_SECRET": SCHEDULE_AGENT_TOKEN_SECRET_NAME,
        }
    }
    logger.info(f"[schedule] Deploying Lambda: {fn_name}")
    try:
        resp = lambda_client.create_function(
            FunctionName=fn_name,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_function.handler",
            Code={"ZipFile": zip_bytes},
            Timeout=60,
            MemorySize=256,
            Environment=env,
            Description=f"my-schedule runner for {project_name}",
            Tags={"Project": project_name, "Name": fn_name},
        )
        arn = resp["FunctionArn"]
        logger.info(f"  ✓ Created Lambda: {arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
        lambda_client.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        # Wait briefly for code update before config update
        time.sleep(2)
        try:
            lambda_client.update_function_configuration(
                FunctionName=fn_name,
                Role=role_arn,
                Timeout=60,
                MemorySize=256,
                Environment=env,
            )
        except ClientError as cfg_err:
            # Concurrent update during code publish — retry once
            if cfg_err.response["Error"]["Code"] == "ResourceConflictException":
                time.sleep(5)
                lambda_client.update_function_configuration(
                    FunctionName=fn_name,
                    Role=role_arn,
                    Timeout=60,
                    MemorySize=256,
                    Environment=env,
                )
            else:
                raise
        arn = lambda_client.get_function(FunctionName=fn_name)["Configuration"][
            "FunctionArn"
        ]
        logger.info(f"  ✓ Updated Lambda: {arn}")

    # Allow EventBridge Scheduler service to invoke (also via role; permission is belt+suspenders)
    try:
        lambda_client.add_permission(
            FunctionName=fn_name,
            StatementId=f"AllowSchedulerInvoke-{project_name}",
            Action="lambda:InvokeFunction",
            Principal="scheduler.amazonaws.com",
            SourceAccount=account_id,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            logger.warning(f"  Lambda permission note: {e}")

    return arn

def attach_ecs_schedule_policy(task_role_name: str) -> None:
    """Allow ECS Web to manage schedule jobs + EventBridge Scheduler."""
    attach_inline_policy(
        task_role_name,
        f"ecs-task-schedule-policy-for-{project_name}",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ScheduleJobsTable",
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:Query",
                    ],
                    "Resource": [
                        f"arn:aws:dynamodb:{region}:{account_id}:table/{SCHEDULE_JOBS_TABLE_NAME}",
                        f"arn:aws:dynamodb:{region}:{account_id}:table/{SCHEDULE_JOBS_TABLE_NAME}/index/*",
                    ],
                },
                {
                    "Sid": "EventBridgeSchedulerManage",
                    "Effect": "Allow",
                    "Action": [
                        "scheduler:CreateSchedule",
                        "scheduler:UpdateSchedule",
                        "scheduler:DeleteSchedule",
                        "scheduler:GetSchedule",
                    ],
                    "Resource": [
                        f"arn:aws:scheduler:{region}:{account_id}:schedule/{SCHEDULE_GROUP_NAME}/*",
                    ],
                },
                {
                    "Sid": "PassSchedulerRole",
                    "Effect": "Allow",
                    "Action": ["iam:PassRole"],
                    "Resource": [
                        f"arn:aws:iam::{account_id}:role/{SCHEDULER_INVOKE_ROLE_NAME}",
                    ],
                    "Condition": {
                        "StringEquals": {
                            "iam:PassedToService": "scheduler.amazonaws.com"
                        }
                    },
                },
            ],
        },
    )
    logger.info(f"  ✓ Attached schedule policy to {task_role_name}")

def deploy_schedule_infrastructure(
    *,
    app_base_url: str,
    ecs_task_role_name: str,
) -> Dict[str, str]:
    """Provision DynamoDB + Lambda + Scheduler group/roles for my-schedule."""
    logger.info("[schedule] Deploying my-schedule infrastructure")
    get_or_create_schedule_agent_token(rotate=False)
    table_name = create_schedule_jobs_table()
    group_name = create_schedule_group()
    lambda_role_arn = create_schedule_lambda_role()
    # IAM role propagation
    time.sleep(8)
    lambda_arn = create_or_update_schedule_lambda(
        role_arn=lambda_role_arn,
        app_base_url=app_base_url,
        table_name=table_name,
    )
    scheduler_role_arn = create_scheduler_invoke_role(lambda_arn)
    if ecs_task_role_name:
        attach_ecs_schedule_policy(ecs_task_role_name)
    info = {
        "schedule_jobs_table": table_name,
        "schedule_group_name": group_name,
        "schedule_lambda_arn": lambda_arn,
        "scheduler_role_arn": scheduler_role_arn,
        "schedule_lambda_role_arn": lambda_role_arn,
    }
    logger.info(f"  ✓ my-schedule infra ready: {info}")
    return info

def _get_installer_iam_arn() -> str:
    """Return IAM ARN for the credentials running this installer.

    Assumed-role sessions (including IAM Identity Center / SSO) are normalized
    to the underlying role ARN, including the role path. A path-less
    ``role/{name}`` ARN is wrong for SSO roles such as
    ``role/aws-reserved/sso.amazonaws.com/.../AWSReservedSSO_*``.
    """
    identity = sts_client.get_caller_identity()
    arn = identity["Arn"]
    if ":assumed-role/" in arn:
        role_name = arn.split(":assumed-role/")[1].split("/")[0]
        try:
            return iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
        except ClientError as e:
            logger.warning(
                f"  Could not resolve full role ARN for {role_name}: {e}; "
                f"falling back to path-less role ARN"
            )
            return f"arn:aws:iam::{identity['Account']}:role/{role_name}"
    return arn


def _opensearch_identity_center_role_arns() -> List[str]:
    """IAM Identity Center (SSO) role ARNs used for AWS Console login.

    Dashboards authenticate as the console principal, which is often an
    ``AWSReservedSSO_*`` role even when the installer itself runs with IAM
    user access keys. Including these roles (not account root) keeps
    Dashboards accessible without widening to ``arn:aws:iam::*:root``.
    """
    role_arns: List[str] = []
    try:
        paginator = iam_client.get_paginator("list_roles")
        for page in paginator.paginate(
            PathPrefix="/aws-reserved/sso.amazonaws.com/"
        ):
            for role in page.get("Roles", []):
                arn = role.get("Arn")
                name = role.get("RoleName", "")
                if arn and name.startswith("AWSReservedSSO_"):
                    role_arns.append(arn)
    except ClientError as e:
        logger.warning(
            f"  Could not list IAM Identity Center roles for OpenSearch "
            f"Dashboards access: {e}"
        )
    return role_arns


def _opensearch_data_access_principals(
    knowledge_base_role_arn: Optional[str] = None,
    ec2_role_arn: Optional[str] = None,
) -> List[str]:
    """Principals that need OpenSearch Serverless data-plane access."""
    principals = [
        _get_installer_iam_arn(),
        *_opensearch_identity_center_role_arns(),
    ]
    if knowledge_base_role_arn:
        principals.append(knowledge_base_role_arn)
    if ec2_role_arn:
        principals.append(ec2_role_arn)
    # De-duplicate while preserving order
    seen = set()
    unique = []
    for principal in principals:
        if principal and principal not in seen:
            seen.add(principal)
            unique.append(principal)
    return unique


def _build_opensearch_data_policy_document(
    collection_name: str, principals: List[str]
) -> List[Dict]:
    """Build OpenSearch Serverless data access policy document."""
    return [
        {
            "Rules": [
                {
                    "Resource": [f"collection/{collection_name}"],
                    "Permission": [
                        "aoss:CreateCollectionItems",
                        "aoss:DeleteCollectionItems",
                        "aoss:UpdateCollectionItems",
                        "aoss:DescribeCollectionItems",
                    ],
                    "ResourceType": "collection",
                },
                {
                    "Resource": [f"index/{collection_name}/*"],
                    "Permission": [
                        "aoss:CreateIndex",
                        "aoss:DeleteIndex",
                        "aoss:UpdateIndex",
                        "aoss:DescribeIndex",
                        "aoss:ReadDocument",
                        "aoss:WriteDocument",
                    ],
                    "ResourceType": "index",
                },
            ],
            "Principal": principals,
        }
    ]


def _ensure_opensearch_data_access_principals(
    data_policy_name: str,
    collection_name: str,
    knowledge_base_role_arn: Optional[str] = None,
    ec2_role_arn: Optional[str] = None,
) -> None:
    """Ensure data access policy grants installer, SSO console, KB, and optional EC2 roles."""
    principals_to_add = _opensearch_data_access_principals(
        knowledge_base_role_arn, ec2_role_arn
    )
    try:
        policy_detail = opensearch_client.get_access_policy(
            name=data_policy_name, type="data"
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        data_policy = _build_opensearch_data_policy_document(
            collection_name, principals_to_add
        )
        opensearch_client.create_access_policy(
            name=data_policy_name,
            type="data",
            policy=json.dumps(data_policy),
        )
        logger.info(f"  Created data access policy: {data_policy_name}")
        logger.info("  Waiting for OpenSearch data access policy to propagate...")
        time.sleep(20)
        return

    current_policy = policy_detail["accessPolicyDetail"]["policy"]
    if isinstance(current_policy, str):
        current_policy = json.loads(current_policy)

    needs_update = False
    for rule in current_policy:
        if "Principal" not in rule:
            continue
        current_principals = rule["Principal"]
        if not isinstance(current_principals, list):
            current_principals = [current_principals]
        for principal in principals_to_add:
            if principal and principal not in current_principals:
                current_principals.append(principal)
                needs_update = True
                logger.info(f"  Adding principal to {data_policy_name}: {principal}")
        rule["Principal"] = current_principals

    if needs_update:
        opensearch_client.update_access_policy(
            name=data_policy_name,
            type="data",
            policy=json.dumps(current_policy),
            policyVersion=policy_detail["accessPolicyDetail"]["policyVersion"],
        )
        logger.info(f"  Updated data access policy: {data_policy_name}")
        logger.info("  Waiting for OpenSearch data access policy to propagate...")
        time.sleep(20)
    else:
        logger.debug(f"  Required principals already present in {data_policy_name}")


def create_opensearch_collection(
    ec2_role_arn: str = None, knowledge_base_role_arn: str = None
) -> Dict[str, str]:
    """Create OpenSearch Serverless collection and policies."""
    logger.info("[4/10] Creating OpenSearch Serverless collection")

    collection_name = vector_index_name
    enc_policy_name = f"enc-{project_name}-{region}"
    net_policy_name = f"net-{project_name}-{region}"
    data_policy_name = f"data-{project_name}"

    # Check if collection already exists first
    try:
        existing_collections = opensearch_client.list_collections()
        for collection in existing_collections.get("collectionSummaries", []):
            if collection["name"] == collection_name and collection["status"] == "ACTIVE":
                logger.warning(f"OpenSearch collection already exists: {collection['name']}")
                collection_arn = collection["arn"]

                collection_details = opensearch_client.batch_get_collection(
                    names=[collection_name]
                )
                collection_detail = collection_details["collectionDetails"][0]
                collection_endpoint = collection_detail.get("collectionEndpoint")

                if not collection_endpoint:
                    logger.info(
                        "  Collection endpoint not yet available, waiting for collection to be ready..."
                    )
                    wait_count = 0
                    while True:
                        response = opensearch_client.batch_get_collection(
                            names=[collection_name]
                        )
                        collection_detail = response["collectionDetails"][0]
                        status = collection_detail.get("status")
                        wait_count += 1
                        if wait_count % 6 == 0:
                            logger.debug(
                                f"  Collection status: {status} (waited {wait_count * 10} seconds)"
                            )

                        if (
                            "collectionEndpoint" in collection_detail
                            and collection_detail["collectionEndpoint"]
                        ):
                            collection_endpoint = collection_detail["collectionEndpoint"]
                            if status == "ACTIVE":
                                break
                        elif status == "ACTIVE":
                            time.sleep(10)
                            response = opensearch_client.batch_get_collection(
                                names=[collection_name]
                            )
                            collection_detail = response["collectionDetails"][0]
                            collection_endpoint = collection_detail.get(
                                "collectionEndpoint"
                            )
                            if collection_endpoint:
                                break

                        if wait_count > 60:
                            raise Exception(
                                f"Timeout waiting for collection endpoint. Collection status: {status}"
                            )
                        time.sleep(10)

                _ensure_opensearch_data_access_principals(
                    data_policy_name,
                    collection_name,
                    knowledge_base_role_arn,
                    ec2_role_arn,
                )

                return {
                    "arn": collection_arn,
                    "endpoint": collection_endpoint,
                }
    except Exception as e:
        logger.debug(f"Error checking existing collections: {e}")

    enc_policy = {
        "Rules": [
            {
                "ResourceType": "collection",
                "Resource": [f"collection/{collection_name}"],
            }
        ],
        "AWSOwnedKey": True,
    }

    try:
        opensearch_client.create_security_policy(
            name=enc_policy_name,
            type="encryption",
            description=f"opensearch encryption policy for {project_name}",
            policy=json.dumps(enc_policy),
        )
        logger.debug(f"Created encryption policy: {enc_policy_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            logger.warning(f"Encryption policy already exists: {enc_policy_name}")
        else:
            logger.error(f"Failed to create encryption policy: {e}")
            raise

    net_policy = [
        {
            "Rules": [
                {
                    "ResourceType": "dashboard",
                    "Resource": [f"collection/{collection_name}"],
                },
                {
                    "ResourceType": "collection",
                    "Resource": [f"collection/{collection_name}"],
                },
            ],
            "AllowFromPublic": True,
        }
    ]

    try:
        opensearch_client.create_security_policy(
            name=net_policy_name,
            type="network",
            description=f"opensearch network policy for {project_name}",
            policy=json.dumps(net_policy),
        )
        logger.debug(f"Created network policy: {net_policy_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            logger.warning(f"Network policy already exists: {net_policy_name}")
        else:
            logger.error(f"Failed to create network policy: {e}")
            raise

    principals = _opensearch_data_access_principals(
        knowledge_base_role_arn, ec2_role_arn
    )
    data_policy = _build_opensearch_data_policy_document(collection_name, principals)

    try:
        opensearch_client.create_access_policy(
            name=data_policy_name,
            type="data",
            policy=json.dumps(data_policy),
        )
        logger.debug(f"Created data access policy: {data_policy_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            logger.warning(f"Data access policy already exists: {data_policy_name}")
            _ensure_opensearch_data_access_principals(
                data_policy_name,
                collection_name,
                knowledge_base_role_arn,
                ec2_role_arn,
            )
        else:
            logger.error(f"Failed to create data access policy: {e}")
            raise

    logger.debug("Waiting for policies to be ready...")
    time.sleep(5)

    try:
        response = opensearch_client.create_collection(
            name=collection_name,
            description=f"opensearch correction for {project_name}",
            type="VECTORSEARCH",
        )
        collection_detail = response["createCollectionDetail"]
        collection_arn = collection_detail["arn"]

        logger.info("  Waiting for collection to be active (this may take a few minutes)...")
        collection_endpoint = None
        wait_count = 0
        while True:
            response = opensearch_client.batch_get_collection(names=[collection_name])
            collection_detail = response["collectionDetails"][0]
            status = collection_detail["status"]
            wait_count += 1
            if wait_count % 6 == 0:
                logger.debug(
                    f"  Collection status: {status} (waited {wait_count * 10} seconds)"
                )

            if "collectionEndpoint" in collection_detail:
                collection_endpoint = collection_detail["collectionEndpoint"]
                if status == "ACTIVE":
                    break
            time.sleep(10)

        logger.debug("Waiting for opensearch correction to be ready...")
        time.sleep(30)

        logger.info(f"✓ OpenSearch collection created: {collection_name}")
        logger.info(f"  Endpoint: {collection_endpoint}")
        return {
            "arn": collection_arn,
            "endpoint": collection_endpoint,
        }

    except ClientError as e:
        if e.response["Error"]["Code"] == "ConflictException":
            logger.warning(f"OpenSearch collection already exists: {collection_name}")
            logger.info("  Waiting for collection endpoint to be available...")
            wait_count = 0
            collection_endpoint = None
            while True:
                response = opensearch_client.batch_get_collection(names=[collection_name])
                collection_detail = response["collectionDetails"][0]
                status = collection_detail.get("status")
                wait_count += 1
                if wait_count % 6 == 0:
                    logger.debug(
                        f"  Collection status: {status} (waited {wait_count * 10} seconds)"
                    )

                if (
                    "collectionEndpoint" in collection_detail
                    and collection_detail["collectionEndpoint"]
                ):
                    collection_endpoint = collection_detail["collectionEndpoint"]
                    if status == "ACTIVE":
                        break
                elif status == "ACTIVE":
                    time.sleep(10)
                    response = opensearch_client.batch_get_collection(
                        names=[collection_name]
                    )
                    collection_detail = response["collectionDetails"][0]
                    collection_endpoint = collection_detail.get("collectionEndpoint")
                    if collection_endpoint:
                        break

                if wait_count > 60:
                    raise Exception(
                        f"Timeout waiting for collection endpoint. Collection status: {status}"
                    )
                time.sleep(10)

            if not collection_endpoint:
                raise Exception("Collection endpoint is not available even after waiting")

            _ensure_opensearch_data_access_principals(
                data_policy_name,
                collection_name,
                knowledge_base_role_arn,
                ec2_role_arn,
            )
            return {
                "arn": collection_detail["arn"],
                "endpoint": collection_endpoint,
            }
        logger.error(f"Failed to create OpenSearch collection: {e}")
        raise


def get_available_cidr_block() -> str:
    """Get an available CIDR block that doesn't conflict with existing VPCs."""
    # Candidate CIDR blocks to try
    candidate_cidrs = [
        "10.20.0.0/16",
        "10.21.0.0/16", 
        "10.22.0.0/16",
        "10.23.0.0/16",
        "10.24.0.0/16",
        "172.16.0.0/16",
        "172.17.0.0/16",
        "172.18.0.0/16",
        "192.168.0.0/16"
    ]
    
    # Get all existing VPC CIDR blocks
    existing_cidrs = set()
    try:
        vpcs = ec2_client.describe_vpcs()
        for vpc in vpcs["Vpcs"]:
            existing_cidrs.add(vpc["CidrBlock"])
            # Also check additional CIDR blocks
            for cidr_assoc in vpc.get("CidrBlockAssociationSet", []):
                existing_cidrs.add(cidr_assoc["CidrBlock"])
    except Exception as e:
        logger.warning(f"Could not check existing VPCs: {e}")
    
    # Find first available CIDR
    for cidr in candidate_cidrs:
        if cidr not in existing_cidrs:
            logger.info(f"Using CIDR block: {cidr}")
            return cidr
    
    # Fallback - this should rarely happen
    logger.warning("All candidate CIDR blocks are in use, using 10.25.0.0/16")
    return "10.25.0.0/16"


def get_or_create_internet_gateway(vpc_id: str) -> str:
    """Get existing Internet Gateway or create a new one for the VPC."""
    igws = ec2_client.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
    )
    
    if igws["InternetGateways"]:
        igw_id = igws["InternetGateways"][0]["InternetGatewayId"]
        logger.debug(f"Found existing Internet Gateway: {igw_id}")
        return igw_id
    
    # Create Internet Gateway if it doesn't exist
    logger.info("  No Internet Gateway found. Creating Internet Gateway...")
    igw_response = ec2_client.create_internet_gateway(
        TagSpecifications=[
            {
                "ResourceType": "internet-gateway",
                "Tags": [{"Key": "Name", "Value": f"igw-{project_name}"}]
            }
        ]
    )
    igw_id = igw_response["InternetGateway"]["InternetGatewayId"]
    ec2_client.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    logger.info(f"  Created and attached Internet Gateway: {igw_id}")
    return igw_id


def wait_for_nat_gateway(nat_gateway_id: str, log_interval: int = 6) -> None:
    """Wait for NAT Gateway to become available."""
    wait_count = 0
    while True:
        response = ec2_client.describe_nat_gateways(NatGatewayIds=[nat_gateway_id])
        state = response["NatGateways"][0]["State"]
        wait_count += 1
        if wait_count % log_interval == 0:
            logger.debug(f"  NAT Gateway status: {state} (waited {wait_count * 10} seconds)")
        if state == "available":
            break
        time.sleep(10)
    logger.debug(f"NAT Gateway is available: {nat_gateway_id}")


def get_or_create_nat_gateway(vpc_id: str, public_subnet_id: str) -> str:
    """Get existing NAT Gateway or create a new one in the public subnet."""
    # Check for existing NAT Gateway by VPC ID
    nat_gateways = ec2_client.describe_nat_gateways(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "state", "Values": ["available", "pending"]}
        ]
    )
    
    # Check if there's a NAT Gateway with our project name tag
    nat_gateway_id = None
    for nat_gw in nat_gateways.get("NatGateways", []):
        # Get tags for this NAT Gateway
        try:
            tags_response = ec2_client.describe_tags(
                Filters=[
                    {"Name": "resource-id", "Values": [nat_gw["NatGatewayId"]]},
                    {"Name": "resource-type", "Values": ["nat-gateway"]}
                ]
            )
            tags = {tag["Key"]: tag["Value"] for tag in tags_response.get("Tags", [])}
            
            # Check if it has our project name tag
            if tags.get("Name") == f"nat-{project_name}":
                nat_gateway_id = nat_gw["NatGatewayId"]
                logger.warning(f"  NAT Gateway already exists: {nat_gateway_id}")
                # Wait if it's still pending
                if nat_gw["State"] == "pending":
                    logger.info("  Waiting for existing NAT Gateway to be available...")
                    wait_for_nat_gateway(nat_gateway_id)
                return nat_gateway_id
        except Exception as e:
            logger.debug(f"  Could not check tags for NAT Gateway {nat_gw['NatGatewayId']}: {e}")
        
        # If no name tag match but there's an available NAT Gateway, use it
        if not nat_gateway_id and nat_gw["State"] == "available":
            nat_gateway_id = nat_gw["NatGatewayId"]
            logger.warning(f"  Found existing NAT Gateway: {nat_gateway_id}")
            return nat_gateway_id
    
    # Create NAT Gateway if it doesn't exist
    logger.info("  Allocating Elastic IP for NAT Gateway...")
    eip_response = ec2_client.allocate_address(Domain="vpc")
    eip_allocation_id = eip_response["AllocationId"]
    
    logger.info("  Creating NAT Gateway (this may take a few minutes)...")
    nat_response = ec2_client.create_nat_gateway(
        SubnetId=public_subnet_id,
        AllocationId=eip_allocation_id
    )
    nat_gateway_id = nat_response["NatGateway"]["NatGatewayId"]
    
    # Tag NAT Gateway
    ec2_client.create_tags(
        Resources=[nat_gateway_id],
        Tags=[{"Key": "Name", "Value": f"nat-{project_name}"}]
    )
    
    # Wait for NAT Gateway to be available
    logger.info("  Waiting for NAT Gateway to be available...")
    wait_for_nat_gateway(nat_gateway_id)
    
    return nat_gateway_id


def wait_for_subnet_available(subnet_id: str, max_wait_time: int = 300) -> bool:
    """Wait for subnet to become available."""
    logger.debug(f"  Waiting for subnet {subnet_id} to become available...")
    start_time = time.time()
    while time.time() - start_time < max_wait_time:
        try:
            response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            if response["Subnets"]:
                state = response["Subnets"][0]["State"]
                if state == "available":
                    logger.debug(f"  Subnet {subnet_id} is now available")
                    return True
                elif state == "pending":
                    logger.debug(f"  Subnet {subnet_id} is still pending, waiting...")
                    time.sleep(5)
                else:
                    logger.warning(f"  Subnet {subnet_id} is in unexpected state: {state}")
                    return False
        except ClientError as e:
            logger.warning(f"  Error checking subnet status: {e}")
            time.sleep(5)
    
    logger.warning(f"  Timeout waiting for subnet {subnet_id} to become available")
    return False


def classify_subnets(subnets: List[Dict], filter_available: bool = False) -> Dict[str, List[str]]:
    """
    Classify subnets into public and private based on naming and route tables.
    
    Args:
        subnets: List of subnet dictionaries from AWS describe_subnets response
        filter_available: If True, only include subnets with State == "available"
    
    Returns:
        Dictionary with 'public_subnets' and 'private_subnets' lists
    """
    public_subnets = []
    private_subnets = []
    
    for subnet in subnets:
        # Filter by availability if requested
        if filter_available and subnet.get("State") != "available":
            continue
        
        subnet_name = ""
        for tag in subnet.get("Tags", []):
            if tag["Key"] == "Name":
                subnet_name = tag["Value"]
                break
        
        if "public" in subnet_name.lower():
            public_subnets.append(subnet["SubnetId"])
        elif "private" in subnet_name.lower():
            private_subnets.append(subnet["SubnetId"])
        else:
            # If no clear naming, use route table to determine
            try:
                route_tables = ec2_client.describe_route_tables(
                    Filters=[{"Name": "association.subnet-id", "Values": [subnet["SubnetId"]]}]
                )
                is_public = False
                for rt in route_tables["RouteTables"]:
                    for route in rt["Routes"]:
                        if route.get("GatewayId", "").startswith("igw-"):
                            is_public = True
                            break
                    if is_public:
                        break
                
                if is_public:
                    public_subnets.append(subnet["SubnetId"])
                else:
                    private_subnets.append(subnet["SubnetId"])
            except Exception as e:
                # If we can't determine, assume private
                logger.debug(f"  Could not check route table for subnet {subnet.get('SubnetId', 'unknown')}: {e}")
                private_subnets.append(subnet["SubnetId"])
    
    return {
        "public_subnets": public_subnets,
        "private_subnets": private_subnets
    }


def create_public_subnets(
    vpc_id: str,
    availability_zones: List[str],
    base_octets: List[str] = None,
    vpc_cidr: str = None,
    count: int = None,
    offset: int = 0,
    existing_cidrs: set = None,
    route_table_id: str = None
) -> List[str]:
    """
    Create public subnets in the specified VPC.
    
    Args:
        vpc_id: VPC ID where subnets will be created
        availability_zones: List of availability zone names
        base_octets: Base network octets for CIDR calculation (e.g., ["10", "0"])
        vpc_cidr: VPC CIDR block (alternative to base_octets)
        count: Number of subnets to create (default: len(availability_zones))
        offset: CIDR offset for subnet numbering (default: 0)
        existing_cidrs: Set of existing CIDR blocks to avoid conflicts
        route_table_id: Optional route table ID to associate with subnets
    
    Returns:
        List of created subnet IDs
    """
    if count is None:
        count = len(availability_zones)
    
    if existing_cidrs is None:
        existing_cidrs = set()
    
    # Calculate base_octets from vpc_cidr if not provided
    if base_octets is None and vpc_cidr:
        vpc_network = ipaddress.ip_network(vpc_cidr)
        base_octets = str(vpc_network.network_address).split('.')
    
    if base_octets is None:
        raise ValueError("Either base_octets or vpc_cidr must be provided")
    
    public_subnets = []
    
    # Pre-calculate subnet networks if vpc_cidr is provided
    subnet_networks = None
    if vpc_cidr:
        vpc_network = ipaddress.ip_network(vpc_cidr)
        subnet_networks = list(vpc_network.subnets(new_prefix=24))
    
    for i, az in enumerate(availability_zones[:count]):
        # Calculate subnet CIDR
        if subnet_networks:
            # Use ipaddress to calculate subnet CIDR
            if offset + i < len(subnet_networks):
                subnet_cidr = str(subnet_networks[offset + i])
            else:
                # Fallback to simple calculation
                subnet_cidr = f"{base_octets[0]}.{base_octets[1]}.{offset + i}.0/24"
        else:
            subnet_cidr = f"{base_octets[0]}.{base_octets[1]}.{offset + i}.0/24"
        
        # Check for CIDR conflicts
        if subnet_cidr in existing_cidrs:
            # Try alternative offsets
            found = False
            for alt_offset in range(10, 30):
                if subnet_networks:
                    if alt_offset < len(subnet_networks):
                        alt_cidr = str(subnet_networks[alt_offset])
                    else:
                        alt_cidr = f"{base_octets[0]}.{base_octets[1]}.{alt_offset}.0/24"
                else:
                    alt_cidr = f"{base_octets[0]}.{base_octets[1]}.{alt_offset}.0/24"
                
                if alt_cidr not in existing_cidrs:
                    subnet_cidr = alt_cidr
                    found = True
                    break
            
            if not found:
                logger.warning(f"  Could not find available CIDR for subnet in {az}, skipping...")
                continue
        
        try:
            subnet_response = ec2_client.create_subnet(
                VpcId=vpc_id,
                CidrBlock=subnet_cidr,
                AvailabilityZone=az,
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {"Key": "Name", "Value": f"public-subnet-for-{project_name}-{len(public_subnets)+1}"},
                            {"Key": "aws-cdk:subnet-type", "Value": "Public"},
                            {"Key": "aws-cdk:subnet-name", "Value": f"public-subnet-for-{project_name}"}
                        ]
                    }
                ]
            )
            subnet_id = subnet_response["Subnet"]["SubnetId"]
            public_subnets.append(subnet_id)
            logger.info(f"  Created public subnet: {subnet_id} in {az} with CIDR {subnet_cidr}")
            
            # Enable auto-assign public IP for public subnets
            ec2_client.modify_subnet_attribute(
                SubnetId=subnet_id,
                MapPublicIpOnLaunch={"Value": True}
            )
            
            # Associate with route table if provided
            if route_table_id:
                try:
                    ec2_client.associate_route_table(
                        RouteTableId=route_table_id,
                        SubnetId=subnet_id
                    )
                except Exception as e:
                    logger.warning(f"  Could not associate subnet {subnet_id} with route table: {e}")
        
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ["InvalidSubnet.Overlap", "InvalidSubnet.Range"]:
                logger.warning(f"  Subnet CIDR {subnet_cidr} conflicts, trying alternative...")
                continue
            else:
                logger.error(f"  Failed to create public subnet in {az}: {e}")
                raise
    
    return public_subnets


def create_security_group(
    vpc_id: str,
    group_name: str,
    description: str,
    ingress_rules: List[Dict] = None
) -> str:
    """
    Create a security group with optional ingress rules.
    
    Args:
        vpc_id: VPC ID where security group will be created
        group_name: Name of the security group
        description: Description of the security group
        ingress_rules: List of ingress rule dictionaries. Each dict should have:
            - IpProtocol: Protocol (e.g., "tcp")
            - FromPort: Starting port
            - ToPort: Ending port
            - IpRanges: List of {"CidrIp": "..."} for CIDR-based rules
            - PrefixListIds: List of {"PrefixListId": "..."} for managed prefix lists
            - UserIdGroupPairs: List of {"GroupId": "..."} for security group-based rules
    
    Returns:
        Security group ID
    """
    try:
        sg_response = ec2_client.create_security_group(
            GroupName=group_name,
            Description=description,
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    "ResourceType": "security-group",
                    "Tags": [{"Key": "Name", "Value": group_name}]
                }
            ]
        )
        sg_id = sg_response["GroupId"]
        logger.debug(f"Created security group: {sg_id} ({group_name})")
        
        # Add ingress rules if provided
        if ingress_rules:
            try:
                ec2_client.authorize_security_group_ingress(
                    GroupId=sg_id,
                    IpPermissions=ingress_rules
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                    logger.warning(f"  Could not add ingress rules to security group {sg_id}: {e}")
        
        return sg_id
    
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidGroup.Duplicate":
            # Security group already exists, try to find it
            logger.debug(f"Security group {group_name} already exists, finding it...")
            sgs = ec2_client.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [group_name]},
                    {"Name": "vpc-id", "Values": [vpc_id]}
                ]
            )
            if sgs["SecurityGroups"]:
                return sgs["SecurityGroups"][0]["GroupId"]
            else:
                raise
        else:
                raise


def get_cloudfront_origin_facing_prefix_list_id() -> str:
    """Return the AWS-managed CloudFront origin-facing prefix list ID for this region."""
    response = ec2_client.describe_managed_prefix_lists(
        Filters=[
            {
                "Name": "prefix-list-name",
                "Values": ["com.amazonaws.global.cloudfront.origin-facing"],
            }
        ]
    )
    prefix_lists = response.get("PrefixLists", [])
    if not prefix_lists:
        raise RuntimeError(
            "CloudFront managed prefix list "
            "'com.amazonaws.global.cloudfront.origin-facing' not found in this region"
        )
    return prefix_lists[0]["PrefixListId"]


def ensure_alb_security_group_cloudfront_ingress(sg_id: str, prefix_list_id: str) -> None:
    """
    Restrict ALB SG HTTP/80 ingress to CloudFront only.

    Adds the managed prefix list rule when missing and removes open 0.0.0.0/0.
    """
    sg = ec2_client.describe_security_groups(GroupIds=[sg_id])["SecurityGroups"][0]
    permissions = sg.get("IpPermissions", [])

    has_cloudfront_ingress = False
    open_world_permissions = []

    for perm in permissions:
        if perm.get("IpProtocol") != "tcp":
            continue
        if perm.get("FromPort") != 80 or perm.get("ToPort") != 80:
            continue

        for prefix in perm.get("PrefixListIds", []):
            if prefix.get("PrefixListId") == prefix_list_id:
                has_cloudfront_ingress = True

        open_cidrs = [
            ip_range
            for ip_range in perm.get("IpRanges", [])
            if ip_range.get("CidrIp") == "0.0.0.0/0"
        ]
        if open_cidrs:
            open_world_permissions.append(
                {
                    "IpProtocol": "tcp",
                    "FromPort": 80,
                    "ToPort": 80,
                    "IpRanges": open_cidrs,
                }
            )

    if not has_cloudfront_ingress:
        try:
            ec2_client.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[
                    {
                        "IpProtocol": "tcp",
                        "FromPort": 80,
                        "ToPort": 80,
                        "PrefixListIds": [{"PrefixListId": prefix_list_id}],
                    }
                ],
            )
            logger.info(
                f"  ✓ ALB SG {sg_id}: added CloudFront prefix list ingress ({prefix_list_id})"
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
                raise

    for permission in open_world_permissions:
        try:
            ec2_client.revoke_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[permission],
            )
            logger.info(f"  ✓ ALB SG {sg_id}: removed open 0.0.0.0/0 HTTP ingress")
        except ClientError as e:
            if e.response["Error"]["Code"] != "InvalidPermission.NotFound":
                raise


def create_alb_security_group(vpc_id: str) -> str:
    """
    Create ALB security group with HTTP ingress from CloudFront only.
    
    Args:
        vpc_id: VPC ID where security group will be created
    
    Returns:
        Security group ID
    """
    prefix_list_id = get_cloudfront_origin_facing_prefix_list_id()
    sg_id = create_security_group(
        vpc_id=vpc_id,
        group_name=f"alb-sg-for-{project_name}",
        description="security group for alb",
        ingress_rules=[
            {
                "IpProtocol": "tcp",
                "FromPort": 80,
                "ToPort": 80,
                "PrefixListIds": [{"PrefixListId": prefix_list_id}],
            }
        ]
    )
    # Existing SGs may still allow 0.0.0.0/0; reconcile on every install/reuse.
    ensure_alb_security_group_cloudfront_ingress(sg_id, prefix_list_id)
    return sg_id


def create_vpc_endpoint(
    vpc_id: str,
    service_name: str,
    subnet_ids: List[str],
    security_group_ids: List[str],
    endpoint_name: str = None,
    check_existing: bool = True
) -> str:
    """
    Create a VPC endpoint if it doesn't already exist.
    
    Args:
        vpc_id: VPC ID where endpoint will be created
        service_name: AWS service name (e.g., "com.amazonaws.region.bedrock-runtime")
        subnet_ids: List of subnet IDs for the endpoint
        security_group_ids: List of security group IDs
        endpoint_name: Optional name tag for the endpoint
        check_existing: Whether to check if endpoint already exists before creating
    
    Returns:
        VPC endpoint ID
    """
    # Check if endpoint already exists
    if check_existing:
        try:
            existing_endpoints = ec2_client.describe_vpc_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "service-name", "Values": [service_name]}
                ]
            )
            if existing_endpoints["VpcEndpoints"]:
                endpoint_id = existing_endpoints["VpcEndpoints"][0]["VpcEndpointId"]
                logger.debug(f"VPC endpoint for {service_name} already exists: {endpoint_id}")
                return endpoint_id
        except Exception as e:
            logger.debug(f"Could not check existing endpoints: {e}")
    
    # Create endpoint
    try:
        logger.debug(f"Creating VPC endpoint for {service_name}")
        tag_specs = []
        if endpoint_name:
            tag_specs = [
                {
                    "ResourceType": "vpc-endpoint",
                    "Tags": [{"Key": "Name", "Value": endpoint_name}]
                }
            ]
        
        endpoint_params = {
            "VpcId": vpc_id,
            "ServiceName": service_name,
            "VpcEndpointType": "Interface",
            "SubnetIds": subnet_ids,
            "SecurityGroupIds": security_group_ids,
            "PrivateDnsEnabled": True
        }
        
        # Only include TagSpecifications if we have tags
        if tag_specs:
            endpoint_params["TagSpecifications"] = tag_specs
        
        endpoint_response = ec2_client.create_vpc_endpoint(**endpoint_params)
        endpoint_id = endpoint_response["VpcEndpoint"]["VpcEndpointId"]
        logger.info(f"Created VPC endpoint for {service_name}: {endpoint_id}")
        return endpoint_id
    
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        logger.error(f"Failed to create VPC endpoint for {service_name}: {e}")
        if error_code in ["DuplicateVpcEndpoint", "InvalidVpcEndpoint.Duplicate"]:
            # Endpoint already exists, try to find it
            try:
                existing_endpoints = ec2_client.describe_vpc_endpoints(
                    Filters=[
                        {"Name": "vpc-id", "Values": [vpc_id]},
                        {"Name": "service-name", "Values": [service_name]}
                    ]
                )
                if existing_endpoints["VpcEndpoints"]:
                    endpoint_id = existing_endpoints["VpcEndpoints"][0]["VpcEndpointId"]
                    logger.debug(f"Found existing VPC endpoint for {service_name}: {endpoint_id}")
                    return endpoint_id
            except Exception:
                pass
        
        if error_code not in ["RouteAlreadyExists", "DuplicateVpcEndpoint", "InvalidVpcEndpoint.Duplicate"]:
            logger.warning(f"Failed to create VPC endpoint for {service_name}: {e}")
            raise
        else:
            # Return None if endpoint already exists and we couldn't find it
            return None


def _get_route_table_ids_for_subnets(subnet_ids: List[str], vpc_id: str) -> List[str]:
    """Return route table IDs associated with subnets, falling back to the VPC main route table."""
    route_table_ids = set()
    for subnet_id in subnet_ids:
        try:
            response = ec2_client.describe_route_tables(
                Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
            )
            if response["RouteTables"]:
                route_table_ids.add(response["RouteTables"][0]["RouteTableId"])
        except Exception as e:
            logger.debug(f"Could not get route table for subnet {subnet_id}: {e}")

    if not route_table_ids:
        try:
            response = ec2_client.describe_route_tables(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "association.main", "Values": ["true"]},
                ]
            )
            if response["RouteTables"]:
                route_table_ids.add(response["RouteTables"][0]["RouteTableId"])
        except Exception as e:
            logger.debug(f"Could not get main route table for VPC {vpc_id}: {e}")

    return list(route_table_ids)


def _ensure_gateway_endpoint_route_tables(endpoint_id: str, route_table_ids: List[str]) -> None:
    """Associate additional route tables with an existing S3 gateway VPC endpoint."""
    if not route_table_ids:
        return
    try:
        response = ec2_client.describe_vpc_endpoints(VpcEndpointIds=[endpoint_id])
        endpoints = response.get("VpcEndpoints", [])
        if not endpoints:
            return
        current_route_tables = set(endpoints[0].get("RouteTableIds", []))
        missing_route_tables = [
            route_table_id
            for route_table_id in route_table_ids
            if route_table_id not in current_route_tables
        ]
        if missing_route_tables:
            ec2_client.modify_vpc_endpoint(
                VpcEndpointId=endpoint_id,
                AddRouteTableIds=missing_route_tables,
            )
            logger.info(
                "  Associated S3 gateway endpoint %s with route tables: %s",
                endpoint_id,
                ", ".join(missing_route_tables),
            )
    except ClientError as e:
        logger.warning(f"  Could not update S3 gateway endpoint route tables: {e}")


def create_s3_gateway_vpc_endpoint(
    vpc_id: str,
    route_table_ids: List[str],
    endpoint_name: str = None,
    check_existing: bool = True,
) -> Optional[str]:
    """Create or reuse an S3 gateway VPC endpoint for private subnet route tables."""
    service_name = f"com.amazonaws.{region}.s3"
    if not route_table_ids:
        logger.warning("  Skipping S3 gateway endpoint: no route tables found")
        return None

    if check_existing:
        try:
            existing_endpoints = ec2_client.describe_vpc_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "service-name", "Values": [service_name]},
                ]
            )
            if existing_endpoints["VpcEndpoints"]:
                endpoint_id = existing_endpoints["VpcEndpoints"][0]["VpcEndpointId"]
                logger.debug(f"S3 gateway VPC endpoint already exists: {endpoint_id}")
                _ensure_gateway_endpoint_route_tables(endpoint_id, route_table_ids)
                return endpoint_id
        except Exception as e:
            logger.debug(f"Could not check existing S3 gateway endpoint: {e}")

    try:
        endpoint_params = {
            "VpcId": vpc_id,
            "ServiceName": service_name,
            "VpcEndpointType": "Gateway",
            "RouteTableIds": route_table_ids,
        }
        if endpoint_name:
            endpoint_params["TagSpecifications"] = [
                {
                    "ResourceType": "vpc-endpoint",
                    "Tags": [{"Key": "Name", "Value": endpoint_name}],
                }
            ]
        endpoint_response = ec2_client.create_vpc_endpoint(**endpoint_params)
        endpoint_id = endpoint_response["VpcEndpoint"]["VpcEndpointId"]
        logger.info(f"Created S3 gateway VPC endpoint: {endpoint_id}")
        return endpoint_id
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ["DuplicateVpcEndpoint", "InvalidVpcEndpoint.Duplicate"]:
            try:
                existing_endpoints = ec2_client.describe_vpc_endpoints(
                    Filters=[
                        {"Name": "vpc-id", "Values": [vpc_id]},
                        {"Name": "service-name", "Values": [service_name]},
                    ]
                )
                if existing_endpoints["VpcEndpoints"]:
                    endpoint_id = existing_endpoints["VpcEndpoints"][0]["VpcEndpointId"]
                    _ensure_gateway_endpoint_route_tables(endpoint_id, route_table_ids)
                    return endpoint_id
            except Exception:
                pass
        logger.warning(f"Failed to create S3 gateway VPC endpoint: {e}")
        raise


def ensure_private_subnet_vpc_endpoints(
    vpc_id: str,
    private_subnets: List[str],
    security_group_id: str,
) -> Dict[str, Optional[str]]:
    """
    Ensure VPC endpoints required for private subnet workloads.

    Interface endpoints: ECR API/DKR (image pull), CloudWatch Logs, Secrets Manager,
    and Bedrock AgentCore data/control planes (ECS + Agent Runtime in private subnets).
    Gateway endpoint: S3 (ECR image layers).
    """
    if not private_subnets or not security_group_id:
        logger.warning("  Skipping VPC endpoint setup: missing private subnets or security group")
        return {}

    logger.info(
        "  Ensuring VPC endpoints for private subnet workloads "
        "(ECR, Logs, Secrets Manager, Bedrock AgentCore, S3)"
    )
    endpoint_ids: Dict[str, Optional[str]] = {}
    interface_services = [
        (f"com.amazonaws.{region}.ecr.api", f"ecr-api-endpoint-{project_name}"),
        (f"com.amazonaws.{region}.ecr.dkr", f"ecr-dkr-endpoint-{project_name}"),
        (f"com.amazonaws.{region}.logs", f"logs-endpoint-{project_name}"),
        (f"com.amazonaws.{region}.secretsmanager", f"secretsmanager-endpoint-{project_name}"),
        (f"com.amazonaws.{region}.bedrock-agentcore", f"bedrock-agentcore-endpoint-{project_name}"),
        (
            f"com.amazonaws.{region}.bedrock-agentcore-control",
            f"bedrock-agentcore-control-endpoint-{project_name}",
        ),
    ]
    for service_name, endpoint_name in interface_services:
        endpoint_ids[service_name] = create_vpc_endpoint(
            vpc_id=vpc_id,
            service_name=service_name,
            subnet_ids=private_subnets,
            security_group_ids=[security_group_id],
            endpoint_name=endpoint_name,
            check_existing=True,
        )

    route_table_ids = _get_route_table_ids_for_subnets(private_subnets, vpc_id)
    endpoint_ids["s3"] = create_s3_gateway_vpc_endpoint(
        vpc_id=vpc_id,
        route_table_ids=route_table_ids,
        endpoint_name=f"s3-endpoint-{project_name}",
    )
    return endpoint_ids


def create_route(
    route_table_id: str,
    destination_cidr: str = "0.0.0.0/0",
    gateway_id: str = None,
    nat_gateway_id: str = None
) -> None:
    """
    Create a route in a route table.
    
    Args:
        route_table_id: Route table ID where the route will be added
        destination_cidr: Destination CIDR block (default: "0.0.0.0/0")
        gateway_id: Internet Gateway ID (for public routes)
        nat_gateway_id: NAT Gateway ID (for private routes)
    
    Note:
        Either gateway_id or nat_gateway_id must be provided, but not both.
    """
    if gateway_id and nat_gateway_id:
        raise ValueError("Cannot specify both gateway_id and nat_gateway_id")
    if not gateway_id and not nat_gateway_id:
        raise ValueError("Either gateway_id or nat_gateway_id must be provided")
    
    route_params = {
        "RouteTableId": route_table_id,
        "DestinationCidrBlock": destination_cidr
    }
    
    if gateway_id:
        route_params["GatewayId"] = gateway_id
    else:
        route_params["NatGatewayId"] = nat_gateway_id
    
    ec2_client.create_route(**route_params)


def _find_route_table_by_name(vpc_id: str, route_table_name: str) -> Optional[str]:
    """Return a route table ID in the VPC that matches the Name tag."""
    try:
        route_tables = ec2_client.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for rt in route_tables.get("RouteTables", []):
            for tag in rt.get("Tags", []):
                if tag.get("Key") == "Name" and tag.get("Value") == route_table_name:
                    return rt["RouteTableId"]
    except ClientError as e:
        logger.debug(f"Could not look up route table {route_table_name}: {e}")
    return None


def _find_route_table_for_nat_gateway(vpc_id: str, nat_gateway_id: str) -> Optional[str]:
    """Return a route table in the VPC that already routes 0.0.0.0/0 to the NAT gateway."""
    try:
        route_tables = ec2_client.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for rt in route_tables.get("RouteTables", []):
            for route in rt.get("Routes", []):
                if (
                    route.get("DestinationCidrBlock") == "0.0.0.0/0"
                    and route.get("NatGatewayId") == nat_gateway_id
                ):
                    return rt["RouteTableId"]
    except ClientError as e:
        logger.debug(f"Could not look up NAT route table for {nat_gateway_id}: {e}")
    return None


def _ensure_nat_default_route(route_table_id: str, nat_gateway_id: str) -> None:
    """Ensure 0.0.0.0/0 in the route table points to the NAT gateway."""
    response = ec2_client.describe_route_tables(RouteTableIds=[route_table_id])
    routes = response["RouteTables"][0].get("Routes", [])
    for route in routes:
        if route.get("DestinationCidrBlock") != "0.0.0.0/0":
            continue
        if route.get("NatGatewayId") == nat_gateway_id:
            return
        ec2_client.replace_route(
            RouteTableId=route_table_id,
            DestinationCidrBlock="0.0.0.0/0",
            NatGatewayId=nat_gateway_id,
        )
        logger.info(
            "  Updated default route on %s to NAT gateway %s",
            route_table_id,
            nat_gateway_id,
        )
        return

    create_route(route_table_id=route_table_id, nat_gateway_id=nat_gateway_id)
    logger.info(
        "  Added default route on %s to NAT gateway %s",
        route_table_id,
        nat_gateway_id,
    )


def _associate_subnet_with_route_table(subnet_id: str, route_table_id: str) -> None:
    """Associate a subnet with a route table, replacing any existing association."""
    try:
        response = ec2_client.describe_route_tables(
            Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
        )
        for rt in response.get("RouteTables", []):
            for assoc in rt.get("Associations", []):
                if assoc.get("SubnetId") != subnet_id:
                    continue
                if assoc.get("RouteTableId") == route_table_id:
                    return
                if not assoc.get("Main", False):
                    ec2_client.disassociate_route_table(
                        AssociationId=assoc["RouteTableAssociationId"]
                    )
    except ClientError as e:
        logger.warning(f"  Could not inspect route association for subnet {subnet_id}: {e}")

    try:
        ec2_client.associate_route_table(RouteTableId=route_table_id, SubnetId=subnet_id)
        logger.info(f"  Associated private subnet {subnet_id} with route table {route_table_id}")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "Resource.AlreadyAssociated":
            return
        logger.warning(f"  Could not associate subnet {subnet_id} with route table {route_table_id}: {e}")


def ensure_private_subnet_nat_routing(
    vpc_id: str,
    public_subnets: List[str],
    private_subnets: List[str],
) -> Optional[str]:
    """
    Ensure private subnets egress via a NAT gateway.

    Creates a NAT gateway in a public subnet when missing, provisions a dedicated
    private route table (0.0.0.0/0 -> NAT), and associates each private subnet with it.
    """
    if not private_subnets:
        logger.debug("  Skipping NAT routing setup: no private subnets")
        return None
    if not public_subnets:
        logger.warning(
            "  Skipping NAT routing setup: no public subnets available for NAT Gateway"
        )
        return None

    logger.info("  Ensuring NAT Gateway and private subnet routing")
    nat_gateway_id = get_or_create_nat_gateway(vpc_id, public_subnets[0])

    private_rt_name = f"private-rt-{project_name}"
    route_table_id = _find_route_table_for_nat_gateway(vpc_id, nat_gateway_id)
    if not route_table_id:
        route_table_id = _find_route_table_by_name(vpc_id, private_rt_name)
    if not route_table_id:
        route_table_id = create_route_table(vpc_id, private_rt_name)
        logger.info(f"  Created private route table: {route_table_id}")

    _ensure_nat_default_route(route_table_id, nat_gateway_id)

    for subnet_id in private_subnets:
        _associate_subnet_with_route_table(subnet_id, route_table_id)

    # Keep the S3 gateway endpoint associated with private route tables as well.
    s3_service = f"com.amazonaws.{region}.s3"
    try:
        endpoints = ec2_client.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "service-name", "Values": [s3_service]},
            ]
        )
        for endpoint in endpoints.get("VpcEndpoints", []):
            _ensure_gateway_endpoint_route_tables(
                endpoint["VpcEndpointId"],
                [route_table_id],
            )
    except ClientError as e:
        logger.debug(f"  Could not update S3 gateway endpoint route tables: {e}")

    logger.info(
        "  ✓ Private subnet NAT routing ready (NAT: %s, route table: %s)",
        nat_gateway_id,
        route_table_id,
    )
    return nat_gateway_id


def create_route_table(vpc_id: str, route_table_name: str) -> str:
    """
    Create a route table with the specified name.
    
    Args:
        vpc_id: VPC ID where the route table will be created
        route_table_name: Name tag for the route table
    
    Returns:
        Route table ID
    """
    response = ec2_client.create_route_table(
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "route-table",
                "Tags": [{"Key": "Name", "Value": route_table_name}]
            }
        ]
    )
    return response["RouteTable"]["RouteTableId"]


def create_vpc_resource(vpc_name: str, cidr_block: str) -> str:
    """
    Create a VPC resource with the specified name and CIDR block.
    
    Args:
        vpc_name: Name tag for the VPC
        cidr_block: CIDR block for the VPC (e.g., "10.0.0.0/16")
    
    Returns:
        VPC ID
    """
    logger.debug(f"Creating VPC: {vpc_name} with CIDR {cidr_block}")
    try:
        response = ec2_client.create_vpc(
            CidrBlock=cidr_block,
            TagSpecifications=[
                {
                    "ResourceType": "vpc",
                    "Tags": [{"Key": "Name", "Value": vpc_name}]
                }
            ]
        )
        vpc_id = response["Vpc"]["VpcId"]
        logger.debug(f"VPC created: {vpc_id}")
        return vpc_id
    except Exception as e:
        logger.error(f"Failed to create VPC: {e}")
        raise


def _is_valid_dhcp_options_id(dhcp_options_id: Optional[str]) -> bool:
    """Return True if dhcp_options_id exists in the current region."""
    if not dhcp_options_id or not str(dhcp_options_id).startswith("dopt-"):
        return False
    try:
        ec2_client.describe_dhcp_options(DhcpOptionsIds=[dhcp_options_id])
        return True
    except ClientError:
        return False


def get_or_create_dhcp_options() -> str:
    """Return a valid regional DHCP options set ID."""
    dhcp_options = ec2_client.describe_dhcp_options().get("DhcpOptions", [])
    if dhcp_options:
        return dhcp_options[0]["DhcpOptionsId"]

    logger.info("  Creating regional DHCP options set...")
    response = ec2_client.create_dhcp_options(
        DhcpConfigurations=[
            {
                "Key": "domain-name-servers",
                "Values": [{"Value": "AmazonProvidedDNS"}],
            }
        ],
        TagSpecifications=[
            {
                "ResourceType": "dhcp-options",
                "Tags": [{"Key": "Name", "Value": f"dhcp-options-for-{project_name}"}],
            }
        ],
    )
    dhcp_options_id = response["DhcpOptions"]["DhcpOptionsId"]
    logger.info(f"  ✓ Created DHCP options set: {dhcp_options_id}")
    return dhcp_options_id


def ensure_vpc_dhcp_options(vpc_id: str) -> None:
    """Ensure the VPC is associated with a valid DHCP options set.

    Fargate/ECS task placement fails with InvalidDhcpOptionID.NotFound when the VPC
    references a missing or literal \"default\" DHCP options ID.
    """
    vpc = ec2_client.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    current_dhcp_id = vpc.get("DhcpOptionsId")
    if _is_valid_dhcp_options_id(current_dhcp_id):
        logger.debug(f"VPC {vpc_id} DHCP options OK: {current_dhcp_id}")
        return

    dhcp_options_id = get_or_create_dhcp_options()
    logger.warning(
        f"VPC {vpc_id} has invalid DHCP options ({current_dhcp_id!r}); "
        f"associating {dhcp_options_id}"
    )
    ec2_client.associate_dhcp_options(DhcpOptionsId=dhcp_options_id, VpcId=vpc_id)
    logger.info(f"  ✓ Associated DHCP options {dhcp_options_id} with VPC {vpc_id}")


def create_private_subnets(
    vpc_id: str,
    availability_zones: List[str],
    base_octets: List[str] = None,
    vpc_cidr: str = None,
    count: int = None,
    offset: int = 2,
    existing_cidrs: set = None,
    route_table_id: str = None,
    nat_gateway_id: str = None,
    wait_for_available: bool = True
) -> List[str]:
    """
    Create private subnets in the specified VPC.
    
    Args:
        vpc_id: VPC ID where subnets will be created
        availability_zones: List of availability zone names
        base_octets: Base network octets for CIDR calculation (e.g., ["10", "0"])
        vpc_cidr: VPC CIDR block (alternative to base_octets)
        count: Number of subnets to create (default: len(availability_zones))
        offset: CIDR offset for subnet numbering (default: 2 to avoid overlap with public subnets)
        existing_cidrs: Set of existing CIDR blocks to avoid conflicts
        route_table_id: Optional route table ID to associate with subnets
        nat_gateway_id: Optional NAT Gateway ID (used to find/create route table if route_table_id not provided)
        wait_for_available: Whether to wait for subnets to become available (default: True)
    
    Returns:
        List of created subnet IDs
    """
    if count is None:
        count = len(availability_zones)
    
    if existing_cidrs is None:
        existing_cidrs = set()
    
    # Calculate base_octets from vpc_cidr if not provided
    if base_octets is None and vpc_cidr:
        vpc_network = ipaddress.ip_network(vpc_cidr)
        base_octets = str(vpc_network.network_address).split('.')
    
    if base_octets is None:
        raise ValueError("Either base_octets or vpc_cidr must be provided")
    
    private_subnets = []
    
    # Pre-calculate subnet networks if vpc_cidr is provided
    subnet_networks = None
    if vpc_cidr:
        vpc_network = ipaddress.ip_network(vpc_cidr)
        subnet_networks = list(vpc_network.subnets(new_prefix=24))
    
    # Find or create private route table if nat_gateway_id is provided
    if route_table_id is None and nat_gateway_id:
        route_tables = ec2_client.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        for rt in route_tables["RouteTables"]:
            for route in rt["Routes"]:
                if route.get("NatGatewayId") == nat_gateway_id:
                    route_table_id = rt["RouteTableId"]
                    break
            if route_table_id:
                break
        
        if not route_table_id:
            # Create private route table
            route_table_id = create_route_table(vpc_id, f"private-rt-{project_name}")
            create_route(route_table_id=route_table_id, nat_gateway_id=nat_gateway_id)
            logger.info(f"  Created private route table: {route_table_id}")
    
    for i, az in enumerate(availability_zones[:count]):
        # Calculate subnet CIDR
        if subnet_networks:
            # Use ipaddress to calculate subnet CIDR
            if offset + i < len(subnet_networks):
                subnet_cidr = str(subnet_networks[offset + i])
            else:
                # Fallback to simple calculation
                subnet_cidr = f"{base_octets[0]}.{base_octets[1]}.{offset + i}.0/24"
        else:
            subnet_cidr = f"{base_octets[0]}.{base_octets[1]}.{offset + i}.0/24"
        
        # Check for CIDR conflicts
        if subnet_cidr in existing_cidrs:
            # Try alternative offsets
            found = False
            for alt_offset in range(10, 30):
                if subnet_networks:
                    if alt_offset < len(subnet_networks):
                        alt_cidr = str(subnet_networks[alt_offset])
                    else:
                        alt_cidr = f"{base_octets[0]}.{base_octets[1]}.{alt_offset}.0/24"
                else:
                    alt_cidr = f"{base_octets[0]}.{base_octets[1]}.{alt_offset}.0/24"
                
                if alt_cidr not in existing_cidrs:
                    subnet_cidr = alt_cidr
                    found = True
                    break
            
            if not found:
                logger.warning(f"  Could not find available CIDR for subnet in {az}, skipping...")
                continue
        
        try:
            subnet_response = ec2_client.create_subnet(
                VpcId=vpc_id,
                CidrBlock=subnet_cidr,
                AvailabilityZone=az,
                TagSpecifications=[
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {"Key": "Name", "Value": f"private-subnet-for-{project_name}-{i+1}"},
                            {"Key": "aws-cdk:subnet-type", "Value": "Private"},
                            {"Key": "aws-cdk:subnet-name", "Value": f"private-subnet-for-{project_name}"}
                        ]
                    }
                ]
            )
            subnet_id = subnet_response["Subnet"]["SubnetId"]
            logger.info(f"  Created private subnet: {subnet_id} in {az} with CIDR {subnet_cidr}")
            
            # Wait for subnet to become available if requested
            if wait_for_available:
                if wait_for_subnet_available(subnet_id):
                    private_subnets.append(subnet_id)
                else:
                    logger.warning(f"  Subnet {subnet_id} did not become available in time, but continuing...")
                    private_subnets.append(subnet_id)  # Still add it, might work anyway
            else:
                private_subnets.append(subnet_id)
            
            # Associate with route table if provided
            if route_table_id:
                try:
                    ec2_client.associate_route_table(
                        RouteTableId=route_table_id,
                        SubnetId=subnet_id
                    )
                except Exception as e:
                    logger.warning(f"  Could not associate subnet {subnet_id} with route table: {e}")
        
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ["InvalidSubnet.Overlap", "InvalidSubnet.Range"]:
                logger.warning(f"  Subnet CIDR {subnet_cidr} conflicts, trying alternative...")
                continue
            else:
                logger.error(f"  Failed to create private subnet in {az}: {e}")
                raise
    
    return private_subnets


def ensure_private_subnets(vpc_id: str, public_subnets: List[str], existing_subnets: List[Dict] = None) -> List[str]:
    """Ensure private subnets exist in VPC, creating them if necessary."""
    private_subnets = []
    
    # Get existing subnets if not provided
    if existing_subnets is None:
        try:
            subnets_response = ec2_client.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            existing_subnets = subnets_response["Subnets"]
        except Exception as e:
            logger.warning(f"Could not retrieve existing subnets: {e}")
            existing_subnets = []
    
    # Check existing subnets for private subnets
    classified = classify_subnets(existing_subnets)
    private_subnets = classified["private_subnets"]
    
    # If no private subnets found, create them automatically
    if not private_subnets:
        logger.info("  No private subnets found. Creating private subnets for ECS deployment...")
        
        # Get VPC CIDR and availability zones
        vpc_detail = ec2_client.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
        vpc_cidr = vpc_detail["CidrBlock"]
        
        # Get availability zones
        azs = ec2_client.describe_availability_zones()["AvailabilityZones"][:2]
        az_names = [az["ZoneName"] for az in azs]
        
        # Get existing subnet CIDRs to avoid conflicts
        existing_cidrs = set()
        for subnet in existing_subnets:
            existing_cidrs.add(subnet["CidrBlock"])
        
        # Parse VPC CIDR to determine subnet CIDRs
        vpc_network = ipaddress.ip_network(vpc_cidr)
        base_octets = str(vpc_network.network_address).split('.')
        
        # Get NAT Gateway (create if needed)
        if not public_subnets:
            raise ValueError(
                "Cannot create private subnets without public subnets for NAT Gateway. "
                "Please ensure your VPC has at least one public subnet."
            )
        nat_gateway_id = get_or_create_nat_gateway(vpc_id, public_subnets[0])
        
        # Create private subnets
        private_subnets = create_private_subnets(
            vpc_id=vpc_id,
            availability_zones=az_names,
            base_octets=base_octets,
            existing_cidrs=existing_cidrs,
            nat_gateway_id=nat_gateway_id,
            wait_for_available=True
        )
        
        if not private_subnets:
            raise ValueError(
                "Failed to create private subnets. "
                "Please ensure your VPC has available CIDR space and try again."
            )
        
        logger.info(f"  ✓ Created {len(private_subnets)} private subnet(s) for ECS deployment")
    
    # Verify private subnets are available (filter out non-available ones)
    available_private_subnets = []
    for subnet_id in private_subnets:
        try:
            subnet_detail = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            if subnet_detail["Subnets"] and subnet_detail["Subnets"][0]["State"] == "available":
                available_private_subnets.append(subnet_id)
            else:
                logger.warning(f"  Private subnet {subnet_id} is not available, waiting...")
                if wait_for_subnet_available(subnet_id):
                    available_private_subnets.append(subnet_id)
        except Exception as e:
            logger.warning(f"  Could not verify subnet {subnet_id}: {e}")
    
    if available_private_subnets:
        private_subnets = available_private_subnets
    elif private_subnets:
        # If we have subnets but they're not available yet, wait a bit
        logger.info("  Waiting for private subnets to become available...")
        time.sleep(10)
        for subnet_id in private_subnets:
            if wait_for_subnet_available(subnet_id, max_wait_time=60):
                available_private_subnets.append(subnet_id)
        if available_private_subnets:
            private_subnets = available_private_subnets

    if private_subnets and public_subnets:
        ensure_private_subnet_nat_routing(vpc_id, public_subnets, private_subnets)

    return private_subnets


def create_vpc() -> Dict[str, str]:
    """Create VPC with subnets and security groups."""
    logger.info("[5/10] Creating VPC and networking resources")
    
    vpc_name = f"vpc-for-{project_name}"
    cidr_block = get_available_cidr_block()
    
    # Check if VPC already exists
    vpcs = ec2_client.describe_vpcs(
        Filters=[{"Name": "tag:Name", "Values": [vpc_name]}]
    )
    if vpcs["Vpcs"]:
        vpc_id = vpcs["Vpcs"][0]["VpcId"]
        logger.warning(f"VPC already exists: {vpc_id}")
        ensure_vpc_dhcp_options(vpc_id)

        try:
            # Get existing resources
            subnets = ec2_client.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            classified = classify_subnets(subnets["Subnets"])
            public_subnets = classified["public_subnets"]
            private_subnets = classified["private_subnets"]
            
            # If no private subnets found, create them automatically
            if not private_subnets:
                private_subnets = ensure_private_subnets(vpc_id, public_subnets, subnets["Subnets"])
            elif public_subnets:
                ensure_private_subnet_nat_routing(vpc_id, public_subnets, private_subnets)

            # Validate that we have at least 2 public subnets (should always be true for VPCs created by this script)
            if len(public_subnets) < 2:
                raise ValueError(
                    f"ALB requires at least 2 public subnets in different availability zones. "
                    f"Found only {len(public_subnets)} public subnet(s) in VPC {vpc_id}. "
                    f"Please ensure your VPC has at least 2 public subnets."
                )
            
            # Validate that public subnets are in different availability zones
            subnet_details = ec2_client.describe_subnets(SubnetIds=public_subnets)
            azs = {subnet["AvailabilityZone"] for subnet in subnet_details["Subnets"]}
            if len(azs) < 2:
                raise ValueError(
                    f"ALB requires subnets in at least 2 different availability zones. "
                    f"Found public subnets only in {len(azs)} availability zone(s): {azs}. "
                    f"Please ensure your VPC has public subnets in at least 2 different availability zones."
                )
            
            # Get security groups
            sgs = ec2_client.describe_security_groups(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            alb_sg_id = None
            ecs_sg_id = None
            for sg in sgs["SecurityGroups"]:
                if sg["GroupName"] != "default":
                    for tag in sg.get("Tags", []):
                        if tag["Key"] == "Name":
                            if f"alb-sg-for-{project_name}" in tag["Value"]:
                                alb_sg_id = sg["GroupId"]
                            elif f"ecs-sg-for-{project_name}" in tag["Value"]:
                                ecs_sg_id = sg["GroupId"]
            
            # If security groups not found, create them
            if not alb_sg_id or not ecs_sg_id:
                logger.info("  Creating missing security groups...")
                if not alb_sg_id:
                    alb_sg_id = create_alb_security_group(vpc_id)
                
                if not ecs_sg_id:
                    ecs_sg_id = create_security_group(
                        vpc_id=vpc_id,
                        group_name=f"ecs-sg-for-{project_name}",
                        description="Security group for ECS tasks",
                        ingress_rules=[
                            {
                                "IpProtocol": "tcp",
                                "FromPort": 8501,
                                "ToPort": 8501,
                                "UserIdGroupPairs": [{"GroupId": alb_sg_id}]
                            }
                        ]
                    )
            
            vpc_endpoint_id = None
            if ecs_sg_id and private_subnets:
                vpc_endpoint_id = create_vpc_endpoint(
                    vpc_id=vpc_id,
                    service_name=f"com.amazonaws.{region}.bedrock-runtime",
                    subnet_ids=private_subnets,
                    security_group_ids=[ecs_sg_id],
                    endpoint_name=f"bedrock-endpoint-{project_name}",
                    check_existing=True,
                )
                ensure_private_subnet_vpc_endpoints(vpc_id, private_subnets, ecs_sg_id)

            return {
                "vpc_id": vpc_id,
                "public_subnets": public_subnets,
                "private_subnets": private_subnets,
                "alb_sg_id": alb_sg_id,
                "ecs_sg_id": ecs_sg_id,
                "vpc_endpoint_id": vpc_endpoint_id
            }
        except Exception as e:
            # If there's an error processing existing VPC, log warning but still return what we have
            logger.warning(f"Error processing existing VPC {vpc_id}: {e}")
            
            try:
                subnets = ec2_client.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )
                classified = classify_subnets(subnets["Subnets"])
                public_subnets = classified["public_subnets"]
                private_subnets = classified["private_subnets"]
            except Exception as subnet_error:
                logger.warning(f"Could not retrieve subnet information: {subnet_error}")
                public_subnets = public_subnets if 'public_subnets' in locals() else []
                private_subnets = private_subnets if 'private_subnets' in locals() else []
        
            # Validate that we have required subnets and create if missing
            # Create public subnets if missing
            if not public_subnets:
                logger.warning(f"  WARNING: No public subnets found in VPC {vpc_id}")
                logger.info("  Attempting to create public subnets...")
                try:
                    # Get VPC CIDR and availability zones
                    vpc_detail = ec2_client.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
                    vpc_cidr = vpc_detail["CidrBlock"]
                    
                    # Get availability zones
                    azs = ec2_client.describe_availability_zones()["AvailabilityZones"][:2]
                    az_names = [az["ZoneName"] for az in azs]
                    
                    # Get existing subnet CIDRs to avoid conflicts
                    existing_cidrs = set()
                    for subnet in subnets["Subnets"]:
                        existing_cidrs.add(subnet["CidrBlock"])
                    
                    # Get or create Internet Gateway
                    igw_id = get_or_create_internet_gateway(vpc_id)
                    
                    # Find or create public route table
                    route_tables = ec2_client.describe_route_tables(
                        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                    )
                    public_rt_id = None
                    for rt in route_tables["RouteTables"]:
                        for route in rt["Routes"]:
                            if route.get("GatewayId", "") == igw_id:
                                public_rt_id = rt["RouteTableId"]
                                break
                        if public_rt_id:
                            break
                    
                    if not public_rt_id and igw_id:
                        # Create public route table
                        public_rt_id = create_route_table(vpc_id, f"public-rt-{project_name}")
                        create_route(route_table_id=public_rt_id, gateway_id=igw_id)
                        logger.info(f"  Created public route table: {public_rt_id}")
                    
                    # Create public subnets
                    created_public_subnets = create_public_subnets(
                        vpc_id=vpc_id,
                        availability_zones=az_names,
                        vpc_cidr=vpc_cidr,
                        existing_cidrs=existing_cidrs,
                        route_table_id=public_rt_id
                    )
                    public_subnets.extend(created_public_subnets)
                    logger.info(f"  ✓ Successfully created {len(created_public_subnets)} public subnet(s)")
                except Exception as e:
                    logger.error(f"  Failed to create public subnets: {e}")
                    logger.warning(f"  ALB creation may fail without public subnets")
            
            # Create private subnets if missing
            if not private_subnets:
                logger.warning(f"  WARNING: No private subnets found in VPC {vpc_id}")
                logger.info("  Attempting to create private subnets...")
                try:
                    if not public_subnets:
                        raise ValueError("Cannot create private subnets without public subnets for NAT Gateway")
                    
                    private_subnets = ensure_private_subnets(vpc_id, public_subnets, subnets["Subnets"])
                    logger.info(f"  ✓ Successfully created {len(private_subnets)} private subnet(s)")
                except Exception as e:
                    logger.error(f"  Failed to create private subnets: {e}")
                    logger.warning(f"  EC2 instance creation may fail without private subnets")
            
            # Get or create security groups if not already set
            if 'alb_sg_id' not in locals() or not alb_sg_id:
                try:
                    sgs = ec2_client.describe_security_groups(
                        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                    )
                    alb_sg_id = None
                    for sg in sgs["SecurityGroups"]:
                        if sg["GroupName"] != "default":
                            for tag in sg.get("Tags", []):
                                if tag["Key"] == "Name" and f"alb-sg-for-{project_name}" in tag["Value"]:
                                    alb_sg_id = sg["GroupId"]
                                    break
                            if alb_sg_id:
                                break
                    
                    if not alb_sg_id:
                        logger.info("  Creating ALB security group...")
                        alb_sg_id = create_alb_security_group(vpc_id)
                except Exception as e:
                    logger.warning(f"  Could not get or create ALB security group: {e}")
                    alb_sg_id = None
            
            if 'ecs_sg_id' not in locals() or not ecs_sg_id:
                try:
                    sgs = ec2_client.describe_security_groups(
                        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                    )
                    ecs_sg_id = None
                    for sg in sgs["SecurityGroups"]:
                        if sg["GroupName"] != "default":
                            for tag in sg.get("Tags", []):
                                if tag["Key"] == "Name" and f"ecs-sg-for-{project_name}" in tag["Value"]:
                                    ecs_sg_id = sg["GroupId"]
                                    break
                            if ecs_sg_id:
                                break
                    
                    if not ecs_sg_id:
                        logger.info("  Creating ECS security group...")
                        # Get VPC CIDR for ingress rule
                        vpc_detail = ec2_client.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
                        vpc_cidr = vpc_detail["CidrBlock"]
                        
                        ecs_sg_id = create_security_group(
                            vpc_id=vpc_id,
                            group_name=f"ecs-sg-for-{project_name}",
                            description="Security group for ECS tasks",
                            ingress_rules=[
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 8501,
                                    "ToPort": 8501,
                                    "UserIdGroupPairs": [{"GroupId": alb_sg_id}] if alb_sg_id else []
                                },
                                {
                                    "IpProtocol": "tcp",
                                    "FromPort": 443,
                                    "ToPort": 443,
                                    "IpRanges": [{"CidrIp": vpc_cidr}]
                                }
                            ]
                        )
                except Exception as e:
                    logger.warning(f"  Could not get or create ECS security group: {e}")
                    ecs_sg_id = None
            
            if ecs_sg_id and private_subnets:
                if 'vpc_endpoint_id' not in locals() or not vpc_endpoint_id:
                    vpc_endpoint_id = create_vpc_endpoint(
                        vpc_id=vpc_id,
                        service_name=f"com.amazonaws.{region}.bedrock-runtime",
                        subnet_ids=private_subnets,
                        security_group_ids=[ecs_sg_id],
                        endpoint_name=f"bedrock-endpoint-{project_name}",
                        check_existing=True,
                    )
                ensure_private_subnet_vpc_endpoints(vpc_id, private_subnets, ecs_sg_id)

            if (
                "public_subnets" in locals()
                and "private_subnets" in locals()
                and public_subnets
                and private_subnets
            ):
                ensure_private_subnet_nat_routing(vpc_id, public_subnets, private_subnets)

            # Return minimal configuration with existing VPC
            return {
                "vpc_id": vpc_id,
                "public_subnets": public_subnets if 'public_subnets' in locals() else [],
                "private_subnets": private_subnets if 'private_subnets' in locals() else [],
                "alb_sg_id": alb_sg_id if 'alb_sg_id' in locals() else None,
                "ecs_sg_id": ecs_sg_id if 'ecs_sg_id' in locals() else None,
                "vpc_endpoint_id": vpc_endpoint_id if 'vpc_endpoint_id' in locals() else None
            }
    
    # No existing VPC found, create new one
    logger.info("No existing VPC found, creating new VPC...")
    
    # Create VPC
    vpc_id = create_vpc_resource(vpc_name, cidr_block)
    
    # Enable DNS hostnames and DNS resolution
    logger.debug("Enabling DNS hostnames and DNS support")
    ec2_client.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2_client.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ensure_vpc_dhcp_options(vpc_id)

    # Get availability zones
    logger.debug("Getting availability zones")
    azs = ec2_client.describe_availability_zones()["AvailabilityZones"][:2]
    az_names = [az["ZoneName"] for az in azs]
    logger.debug(f"Using availability zones: {az_names}")

    # Parse CIDR to get base network for subnet creation
    vpc_network = ipaddress.ip_network(cidr_block)
    base_octets = str(vpc_network.network_address).split('.')

    # Create Internet Gateway
    logger.debug("Creating Internet Gateway")
    igw_id = get_or_create_internet_gateway(vpc_id)

    # Create public subnets
    logger.debug("Creating public subnets")
    public_subnets = create_public_subnets(
        vpc_id=vpc_id,
        availability_zones=az_names,
        base_octets=base_octets,
        offset=0
    )

    # Create NAT Gateway in first public subnet
    logger.debug("Creating NAT Gateway")
    nat_gateway_id = get_or_create_nat_gateway(vpc_id, public_subnets[0])
    
    # Create route tables
    logger.debug("Creating route tables")
    public_rt_id = create_route_table(vpc_id, f"public-rt-{project_name}")
    
    # Add route to Internet Gateway
    create_route(route_table_id=public_rt_id, gateway_id=igw_id)
    
    # Associate public subnets with public route table
    for subnet_id in public_subnets:
        ec2_client.associate_route_table(
            RouteTableId=public_rt_id,
            SubnetId=subnet_id
        )
    
    # Create private subnets (with NAT Gateway and route table setup)
    logger.debug("Creating private subnets")
    private_subnets = create_private_subnets(
        vpc_id=vpc_id,
        availability_zones=az_names,
        base_octets=base_octets,
        offset=2,
        nat_gateway_id=nat_gateway_id,
        wait_for_available=True
    )
    
    # Create security groups first (needed for VPC endpoints)
    logger.debug("Creating security groups")
    
    # Create ALB security group
    alb_sg_id = create_alb_security_group(vpc_id)
    logger.debug(f"ALB security group created: {alb_sg_id}")
    
    # Create ECS security group
    ecs_sg_id = create_security_group(
        vpc_id=vpc_id,
        group_name=f"ecs-sg-for-{project_name}",
        description="Security group for ECS tasks",
        ingress_rules=[
            {
                "IpProtocol": "tcp",
                "FromPort": 8501,
                "ToPort": 8501,
                "UserIdGroupPairs": [{"GroupId": alb_sg_id}]
            },
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": cidr_block}]
            }
        ]
    )
    logger.debug(f"ECS security group created: {ecs_sg_id}")
    
    # Create VPC endpoints for Bedrock and private subnet workloads
    logger.debug("Creating VPC endpoints")

    vpc_endpoint_id = create_vpc_endpoint(
        vpc_id=vpc_id,
        service_name=f"com.amazonaws.{region}.bedrock-runtime",
        subnet_ids=private_subnets,
        security_group_ids=[ecs_sg_id],
        endpoint_name=f"bedrock-endpoint-{project_name}",
        check_existing=True,
    )
    ensure_private_subnet_vpc_endpoints(vpc_id, private_subnets, ecs_sg_id)

    logger.debug("VPC endpoints created")
    
    logger.info(f"✓ VPC created: {vpc_id}")
    
    return {
        "vpc_id": vpc_id,
        "public_subnets": public_subnets,
        "private_subnets": private_subnets,
        "alb_sg_id": alb_sg_id,
        "ecs_sg_id": ecs_sg_id,
        "vpc_endpoint_id": vpc_endpoint_id
    }


def create_alb(vpc_info: Dict[str, str]) -> Dict[str, str]:
    """Create Application Load Balancer."""
    logger.info("[6/10] Creating Application Load Balancer")
    alb_name = f"alb-for-{project_name}"
    
    # Check if ALB already exists
    try:
        albs = elbv2_client.describe_load_balancers(Names=[alb_name])
        if albs["LoadBalancers"]:
            alb = albs["LoadBalancers"][0]
            logger.warning(f"ALB already exists: {alb['DNSName']}")
            ensure_alb_idle_timeout(alb["LoadBalancerArn"])
            return {
                "arn": alb["LoadBalancerArn"],
                "dns": alb["DNSName"]
            }
    except ClientError as e:
        if e.response["Error"]["Code"] != "LoadBalancerNotFound":
            raise
    
    # Validate that we have at least 2 subnets in different availability zones
    public_subnets = vpc_info["public_subnets"]
    
    # If no public subnets provided, try to find them from VPC
    if not public_subnets:
        logger.warning("  No public subnets found in vpc_info. Attempting to find public subnets from VPC...")
        try:
            subnets = ec2_client.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_info["vpc_id"]]}]
            )
            all_subnets = []
            
            # Collect subnet info for logging
            for subnet in subnets["Subnets"]:
                subnet_name = ""
                for tag in subnet.get("Tags", []):
                    if tag["Key"] == "Name":
                        subnet_name = tag["Value"]
                        break
                
                subnet_info = {
                    "id": subnet["SubnetId"],
                    "name": subnet_name,
                    "az": subnet["AvailabilityZone"],
                    "cidr": subnet["CidrBlock"]
                }
                all_subnets.append(subnet_info)
            
            # Classify subnets
            classified = classify_subnets(subnets["Subnets"])
            public_subnets = classified["public_subnets"]
            private_subnets = classified["private_subnets"]
            
            # Log all subnets found for debugging
            if all_subnets:
                logger.info(f"  Found {len(all_subnets)} subnet(s) in VPC:")
                for subnet_info in all_subnets:
                    logger.info(f"    - {subnet_info['id']}: {subnet_info['name']} ({subnet_info['az']}, {subnet_info['cidr']})")
                logger.info(f"  Identified {len(public_subnets)} public subnet(s) and {len(private_subnets)} private subnet(s)")
            else:
                logger.warning(f"  No subnets found in VPC {vpc_info['vpc_id']}")
                
        except Exception as e:
            logger.error(f"  Could not retrieve subnets from VPC: {e}")
            raise
    
    
    # Ensure ALB security group exists
    alb_sg_id = vpc_info.get("alb_sg_id")
    if not alb_sg_id:
        logger.info("  ALB security group not found. Creating ALB security group...")
        vpc_id = vpc_info["vpc_id"]
        alb_sg_id = create_alb_security_group(vpc_id)
        logger.info(f"  ✓ Created ALB security group: {alb_sg_id}")
    
    # Get availability zones for logging
    subnet_details = ec2_client.describe_subnets(SubnetIds=public_subnets)
    azs = {subnet["AvailabilityZone"] for subnet in subnet_details["Subnets"]}
    
    logger.debug(f"Creating ALB: {alb_name} with {len(public_subnets)} subnets in {len(azs)} availability zones")
    response = elbv2_client.create_load_balancer(
        Name=alb_name,
        Subnets=public_subnets,
        SecurityGroups=[alb_sg_id],
        Scheme="internet-facing",
        Type="application",
        Tags=[
            {"Key": "Name", "Value": alb_name}
        ]
    )
    
    alb_arn = response["LoadBalancers"][0]["LoadBalancerArn"]
    alb_dns = response["LoadBalancers"][0]["DNSName"]
    
    logger.info(f"✓ ALB created: {alb_dns}")
    ensure_alb_idle_timeout(alb_arn)
    
    return {
        "arn": alb_arn,
        "dns": alb_dns
    }


def create_lambda_role() -> str:
    """Create Lambda RAG IAM role (legacy helper; not used by main deploy path)."""
    logger.info("[2/10] Creating Lambda RAG IAM role")
    role_name = f"role-lambda-rag-for-{project_name}-{region}"
    bucket_arn, object_arn = _project_s3_bucket_arns()
    
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": ["lambda.amazonaws.com", "bedrock.amazonaws.com"]
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    role_arn = create_iam_role(role_name, assume_role_policy)

    
    create_log_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup"],
                "Resource": [f"arn:aws:logs:{region}:{account_id}:*"]
            }
        ]
    }
    attach_inline_policy(role_name, f"create-log-policy-lambda-rag-for-{project_name}", create_log_policy)
    
    create_log_stream_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [f"arn:aws:logs:{region}:{account_id}:log-group:/aws/lambda/*"]
            }
        ]
    }
    attach_inline_policy(role_name, f"create-stream-log-policy-lambda-rag-for-{project_name}", create_log_stream_policy)
    
    bedrock_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeModelsAndRetrieve",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account_id}:inference-profile/*",
                    f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*",
                ],
            }
        ],
    }
    attach_inline_policy(role_name, f"lambda-rag-bedrock-policy-for-{project_name}", bedrock_policy)
    
    opensearch_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": [f"arn:aws:aoss:{region}:{account_id}:collection/*"],
            }
        ],
    }
    attach_inline_policy(role_name, f"lambda-rag-opensearch-policy-for-{project_name}", opensearch_policy)

    s3_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": [bucket_arn],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [object_arn],
            },
        ],
    }
    attach_inline_policy(role_name, f"lambda-rag-s3-policy-for-{project_name}", s3_policy)
    
    return role_arn


def check_knowledge_base_exists() -> Optional[str]:
    """Check if Knowledge Base exists and return its ID if found."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
    
    try:
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.debug(f"Knowledge Base found: {kb['knowledgeBaseId']}")
                return kb["knowledgeBaseId"]
        return None
    except Exception as e:
        logger.debug(f"Error checking Knowledge Base existence: {e}")
        return None


def delete_knowledge_base(knowledge_base_id: str) -> None:
    """Delete Knowledge Base and its data sources."""
    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
    
    try:
        # Delete all data sources first
        try:
            data_sources = bedrock_agent_client.list_data_sources(
                knowledgeBaseId=knowledge_base_id,
                maxResults=100
            )
            for ds in data_sources.get("dataSourceSummaries", []):
                try:
                    bedrock_agent_client.delete_data_source(
                        knowledgeBaseId=knowledge_base_id,
                        dataSourceId=ds["dataSourceId"]
                    )
                    logger.debug(f"Deleted data source: {ds['dataSourceId']}")
                except Exception as e:
                    logger.warning(f"Failed to delete data source {ds['dataSourceId']}: {e}")
        except Exception as e:
            logger.debug(f"Error listing/deleting data sources: {e}")
        
        # Delete the knowledge base
        bedrock_agent_client.delete_knowledge_base(knowledgeBaseId=knowledge_base_id)
        logger.info(f"Deleted Knowledge Base: {knowledge_base_id}")
        
        # Wait for deletion to complete
        logger.debug("Waiting for Knowledge Base deletion to complete...")
        max_wait = 60  # Wait up to 60 seconds
        waited = 0
        while waited < max_wait:
            try:
                kb_response = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
                status = kb_response["knowledgeBase"]["status"]
                if status == "DELETED":
                    break
                time.sleep(5)
                waited += 5
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    logger.debug("Knowledge Base deletion confirmed")
                    break
                raise
        
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            logger.debug(f"Knowledge Base {knowledge_base_id} already deleted")
        else:
            logger.error(f"Failed to delete Knowledge Base {knowledge_base_id}: {e}")
            raise


def create_s3_vectors_store() -> Dict[str, str]:
    """Create S3 vector bucket and index for Bedrock Knowledge Base."""
    logger.info("[4/10] Creating S3 Vectors store (vector bucket + index)")

    vector_bucket_arn = s3_vectors_bucket_arn()
    index_arn = s3_vectors_index_arn()

    try:
        s3vectors_client.create_vector_bucket(vectorBucketName=vector_bucket_name)
        logger.info(f"  ✓ Vector bucket created: {vector_bucket_name}")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ResourceAlreadyExistsException"):
            logger.warning(f"  Vector bucket already exists: {vector_bucket_name}")
            try:
                existing = s3vectors_client.get_vector_bucket(
                    vectorBucketName=vector_bucket_name
                )
                vector_bucket_arn = existing["vectorBucket"]["vectorBucketArn"]
            except ClientError:
                pass
        else:
            logger.error(f"Failed to create vector bucket: {e}")
            raise

    try:
        response = s3vectors_client.create_index(
            vectorBucketName=vector_bucket_name,
            indexName=vector_index_name,
            dataType=embedding_data_type,
            dimension=embedding_dimensions,
            distanceMetric=distance_metric,
            metadataConfiguration={
                "nonFilterableMetadataKeys": BEDROCK_NON_FILTERABLE_METADATA_KEYS,
            },
        )
        index_arn = response.get("indexArn", index_arn)
        logger.info(f"  ✓ Vector index created: {vector_index_name}")
        logger.info("  Waiting for vector index to be ready...")
        time.sleep(15)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConflictException", "ResourceAlreadyExistsException"):
            logger.warning(f"  Vector index already exists: {vector_index_name}")
            try:
                existing = s3vectors_client.get_index(
                    vectorBucketName=vector_bucket_name,
                    indexName=vector_index_name,
                )
                index_arn = existing["index"]["indexArn"]
            except ClientError:
                pass
        else:
            logger.error(f"Failed to create vector index: {e}")
            raise

    logger.info("✓ S3 Vectors store ready")
    logger.info(f"  Vector bucket ARN: {vector_bucket_arn}")
    logger.info(f"  Vector index ARN: {index_arn}")

    return {
        "vectorBucketName": vector_bucket_name,
        "vectorBucketArn": vector_bucket_arn,
        "indexName": vector_index_name,
        "indexArn": index_arn,
    }


def ensure_data_source(
    bedrock_agent_client,
    knowledge_base_id: str,
    s3_bucket_name: str,
) -> str:
    """Create S3 data source with hierarchical chunking and foundation-model parsing."""
    data_sources = bedrock_agent_client.list_data_sources(
        knowledgeBaseId=knowledge_base_id,
        maxResults=100,
    )
    for ds in data_sources.get("dataSourceSummaries", []):
        if ds["name"] == s3_bucket_name:
            logger.info(f"  Data source already exists: {ds['dataSourceId']}")
            return ds["dataSourceId"]

    # parsing_model_arn = f"arn:aws:bedrock:{region}:{account_id}:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    parsing_model_arn = (
        f"arn:aws:bedrock:{region}:{account_id}:"
        "inference-profile/global.anthropic.claude-sonnet-4-6"
    )

    logger.info("  Creating data source with hierarchical chunking + foundation model parsing...")
    data_source_response = bedrock_agent_client.create_data_source(
        knowledgeBaseId=knowledge_base_id,
        name=s3_bucket_name,
        description=f"S3 data source: {s3_bucket_name}",
        dataDeletionPolicy="RETAIN",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{s3_bucket_name}",
                "inclusionPrefixes": ["docs/"],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "HIERARCHICAL",
                "hierarchicalChunkingConfiguration": {
                    "levelConfigurations": [
                        {"maxTokens": 1500},
                        {"maxTokens": 300},
                    ],
                    "overlapTokens": 60,
                },
            },
            "parsingConfiguration": {
                "parsingStrategy": "BEDROCK_FOUNDATION_MODEL",
                "bedrockFoundationModelConfiguration": {
                    "modelArn": parsing_model_arn,
                },
            },
        },
    )
    data_source_id = data_source_response["dataSource"]["dataSourceId"]
    logger.info(f"  ✓ Data source created: {data_source_id}")
    return data_source_id


def create_vector_index_in_opensearch(collection_endpoint: str, index_name: str) -> bool:
    """Create vector index in OpenSearch Serverless collection."""
    try:
        if not collection_endpoint or not collection_endpoint.strip():
            logger.error(
                f"  Invalid collection endpoint: '{collection_endpoint}'. "
                "Collection endpoint is required."
            )
            return False

        if not collection_endpoint.startswith(("http://", "https://")):
            logger.error(
                f"  Invalid collection endpoint format: '{collection_endpoint}'. "
                "Must start with http:// or https://"
            )
            return False

        try:
            import requests
            from requests_aws4auth import AWS4Auth
        except ImportError:
            logger.info("  Installing required packages for OpenSearch index creation...")
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "requests-aws4auth"]
            )
            import requests
            from requests_aws4auth import AWS4Auth

        session = boto3.Session()
        credentials = session.get_credentials()
        awsauth = AWS4Auth(
            credentials.access_key,
            credentials.secret_key,
            region,
            "aoss",
            session_token=credentials.token,
        )

        url = f"{collection_endpoint}/{index_name}"
        for attempt in range(6):
            response = requests.get(url, auth=awsauth, timeout=30)
            if response.status_code == 200:
                logger.debug(f"Vector index '{index_name}' already exists")
                return True
            if response.status_code in (401, 403) and attempt < 5:
                wait_seconds = 10 * (attempt + 1)
                logger.info(
                    f"  OpenSearch returned {response.status_code}; "
                    f"waiting {wait_seconds}s for data access policy propagation "
                    f"(attempt {attempt + 1}/5)..."
                )
                time.sleep(wait_seconds)
                continue
            if response.status_code == 401:
                logger.error(
                    "  Unauthorized (401) accessing OpenSearch. "
                    f"Ensure {_get_installer_iam_arn()} (and any IAM Identity Center "
                    "console roles) are in the collection data access policy."
                )
                return False
            if response.status_code == 403:
                logger.error(f"  Forbidden (403) accessing OpenSearch index '{index_name}'")
                return False
            break

        index_mapping = {
            "settings": {
                "index": {
                    "knn": True,
                    "knn.algo_param.ef_search": 512,
                }
            },
            "mappings": {
                "properties": {
                    "vector_field": {
                        "type": "knn_vector",
                        "dimension": embedding_dimensions,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "faiss",
                            "parameters": {
                                "ef_construction": 512,
                                "m": 16,
                            },
                        },
                    },
                    "AMAZON_BEDROCK_TEXT": {"type": "text"},
                    "AMAZON_BEDROCK_METADATA": {"type": "text"},
                }
            },
        }

        headers = {"Content-Type": "application/json"}
        response = requests.put(
            url,
            auth=awsauth,
            headers=headers,
            data=json.dumps(index_mapping),
            timeout=30,
        )

        if response.status_code in [200, 201]:
            logger.info(f"  ✓ Vector index '{index_name}' created successfully")
            logger.info("  Waiting for index to be ready...")
            time.sleep(30)
            return True

        logger.error(
            f"  Failed to create vector index: {response.status_code} - {response.text}"
        )
        return False

    except ImportError:
        logger.error(
            "  requests-aws4auth package is required. "
            "Install with: pip install requests-aws4auth"
        )
        return False
    except Exception as e:
        logger.error(f"  Error creating vector index: {e}")
        return False


def create_knowledge_base_with_opensearch(
    opensearch_info: Dict[str, str], knowledge_base_role_arn: str, s3_bucket_name: str
) -> Tuple[str, str]:
    """Create Knowledge Base with OpenSearch Serverless as the vector store."""
    logger.info("[4.5/10] Creating Knowledge Base with OpenSearch collection")

    logger.info("  Creating vector index in OpenSearch collection...")
    if not create_vector_index_in_opensearch(
        opensearch_info["endpoint"], vector_index_name
    ):
        raise Exception("Failed to create vector index in OpenSearch collection")

    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        logger.info("  Checking if Knowledge Base already exists...")
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.warning(f"Knowledge Base already exists: {kb['knowledgeBaseId']}")

                kb_details = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=kb["knowledgeBaseId"]
                )
                storage = kb_details["knowledgeBase"]["storageConfiguration"]
                storage_type = storage.get("type")
                opensearch_cfg = storage.get("opensearchServerlessConfiguration", {})
                kb_collection_arn = opensearch_cfg.get("collectionArn")

                if (
                    storage_type != "OPENSEARCH_SERVERLESS"
                    or kb_collection_arn != opensearch_info["arn"]
                ):
                    logger.warning(
                        "Knowledge Base is not using the expected OpenSearch collection:"
                    )
                    logger.warning(f"  Storage type: {storage_type}")
                    logger.warning(f"  Current collection ARN: {kb_collection_arn}")
                    logger.warning(f"  Expected collection ARN: {opensearch_info['arn']}")
                    delete_knowledge_base(kb["knowledgeBaseId"])
                    break

                logger.info("Knowledge Base is using correct OpenSearch collection")
                data_source_id = ensure_data_source(
                    bedrock_agent_client, kb["knowledgeBaseId"], s3_bucket_name
                )
                return kb["knowledgeBaseId"], data_source_id
        logger.info("  Knowledge Base does not exist. Creating new one...")
    except Exception as e:
        logger.debug(f"Error checking existing Knowledge Base: {e}")

    logger.info("  Verifying Knowledge Base role configuration...")
    try:
        role_response = iam_client.get_role(
            RoleName=f"role-knowledge-base-for-{project_name}-{region}"
        )
        policy_doc = role_response["Role"]["AssumeRolePolicyDocument"]
        if isinstance(policy_doc, str):
            trust_policy = json.loads(policy_doc)
        else:
            trust_policy = policy_doc
        logger.debug(f"  Role trust policy: {json.dumps(trust_policy, indent=2)}")

        statements = trust_policy.get("Statement", [])
        bedrock_allowed = False
        for statement in statements:
            if statement.get("Effect") == "Allow":
                principal = statement.get("Principal", {})
                if principal.get("Service") == "bedrock.amazonaws.com":
                    bedrock_allowed = True
                    break

        if not bedrock_allowed:
            logger.error(
                "  ✗ Knowledge Base role trust policy does not allow bedrock.amazonaws.com"
            )
            logger.error(
                "  Please update the role trust policy manually or delete and recreate the role"
            )
            raise Exception("Knowledge Base role trust policy is incorrect")

        logger.info("  ✓ Knowledge Base role trust policy is correct")
    except ClientError as role_error:
        logger.error(f"  ✗ Failed to verify Knowledge Base role: {role_error}")
        raise

    logger.debug(
        f"Creating Knowledge Base with OpenSearch collection: {opensearch_info['arn']}"
    )
    response = bedrock_agent_client.create_knowledge_base(
        name=project_name,
        description="Knowledge base based on OpenSearch",
        roleArn=knowledge_base_role_arn,
        tags={project_name: "true"},
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
                "embeddingModelConfiguration": {
                    "bedrockEmbeddingModelConfiguration": {
                        "dimensions": embedding_dimensions,
                    }
                },
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": opensearch_info["arn"],
                "fieldMapping": {
                    "metadataField": "AMAZON_BEDROCK_METADATA",
                    "textField": "AMAZON_BEDROCK_TEXT",
                    "vectorField": "vector_field",
                },
                "vectorIndexName": vector_index_name,
            },
        },
    )

    knowledge_base_id = response["knowledgeBase"]["knowledgeBaseId"]
    logger.info(f"✓ Knowledge Base created: {knowledge_base_id}")

    logger.info("  Waiting for Knowledge Base to be active...")
    while True:
        kb_response = bedrock_agent_client.get_knowledge_base(
            knowledgeBaseId=knowledge_base_id
        )
        status = kb_response["knowledgeBase"]["status"]

        if status == "ACTIVE":
            logger.info("  Knowledge Base is now active")
            break
        if status == "FAILED":
            raise Exception("Knowledge Base creation failed")

        logger.debug(f"  Knowledge Base status: {status} (waiting...)")
        time.sleep(10)

    data_source_id = ensure_data_source(
        bedrock_agent_client, knowledge_base_id, s3_bucket_name
    )
    return knowledge_base_id, data_source_id


def create_knowledge_base_with_s3_vectors(
    s3_vectors_info: Dict[str, str], knowledge_base_role_arn: str, s3_bucket_name: str
) -> Tuple[str, str]:
    """Legacy: Create Knowledge Base with S3 Vectors as the vector store."""
    logger.info("[4.5/10] Creating Knowledge Base with S3 Vectors")

    bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)

    try:
        logger.info("  Checking if Knowledge Base already exists...")
        kb_list = bedrock_agent_client.list_knowledge_bases()
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb["name"] == project_name:
                logger.warning(f"Knowledge Base already exists: {kb['knowledgeBaseId']}")

                kb_details = bedrock_agent_client.get_knowledge_base(
                    knowledgeBaseId=kb["knowledgeBaseId"]
                )
                storage = kb_details["knowledgeBase"]["storageConfiguration"]
                s3_cfg = storage.get("s3VectorsConfiguration", {})
                kb_index_arn = s3_cfg.get("indexArn")
                storage_type = storage.get("type")

                if storage_type != "S3_VECTORS" or kb_index_arn != s3_vectors_info["indexArn"]:
                    logger.warning("Knowledge Base is not using the expected S3 Vectors index:")
                    logger.warning(f"  Storage type: {storage_type}")
                    logger.warning(f"  Current index ARN: {kb_index_arn}")
                    logger.warning(f"  Expected index ARN: {s3_vectors_info['indexArn']}")

                    delete_knowledge_base(kb["knowledgeBaseId"])
                    break

                logger.info("Knowledge Base is using correct S3 Vectors index")
                data_source_id = ensure_data_source(
                    bedrock_agent_client, kb["knowledgeBaseId"], s3_bucket_name
                )
                return kb["knowledgeBaseId"], data_source_id
        logger.info("  Knowledge Base does not exist. Creating new one...")
    except Exception as e:
        logger.debug(f"Error checking existing Knowledge Base: {e}")

    logger.info("  Verifying Knowledge Base role configuration...")
    try:
        role_response = iam_client.get_role(
            RoleName=f"role-knowledge-base-for-{project_name}-{region}"
        )
        policy_doc = role_response["Role"]["AssumeRolePolicyDocument"]
        if isinstance(policy_doc, str):
            trust_policy = json.loads(policy_doc)
        else:
            trust_policy = policy_doc
        logger.debug(f"  Role trust policy: {json.dumps(trust_policy, indent=2)}")

        statements = trust_policy.get("Statement", [])
        bedrock_allowed = False
        for statement in statements:
            if statement.get("Effect") == "Allow":
                principal = statement.get("Principal", {})
                if _principal_allows_service(principal, "bedrock.amazonaws.com"):
                    bedrock_allowed = True
                    break

        if not bedrock_allowed:
            logger.error("  ✗ Knowledge Base role trust policy does not allow bedrock.amazonaws.com")
            logger.error("  Please update the role trust policy manually or delete and recreate the role")
            raise Exception("Knowledge Base role trust policy is incorrect")

        logger.info("  ✓ Knowledge Base role trust policy is correct")
    except ClientError as role_error:
        logger.error(f"  ✗ Failed to verify Knowledge Base role: {role_error}")
        raise

    logger.debug(f"Creating Knowledge Base with S3 Vectors index: {s3_vectors_info['indexArn']}")
    kb_create_params = dict(
        name=project_name,
        description="Knowledge base with default parser (S3 Vectors)",
        roleArn=knowledge_base_role_arn,
        tags={project_name: "true"},
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0",
                "embeddingModelConfiguration": {
                    "bedrockEmbeddingModelConfiguration": {
                        "dimensions": embedding_dimensions,
                        "embeddingDataType": "FLOAT32",
                    }
                },
            },
        },
        storageConfiguration={
            "type": "S3_VECTORS",
            "s3VectorsConfiguration": {
                "vectorBucketArn": s3_vectors_info["vectorBucketArn"],
                "indexArn": s3_vectors_info["indexArn"],
            },
        },
    )

    max_retries = 6
    response = None
    for attempt in range(max_retries):
        try:
            response = bedrock_agent_client.create_knowledge_base(**kb_create_params)
            break
        except ClientError as e:
            error_message = e.response.get("Error", {}).get("Message", str(e))
            if (
                e.response.get("Error", {}).get("Code") == "ValidationException"
                and "unable to assume the given role" in error_message.lower()
                and attempt < max_retries - 1
            ):
                wait_seconds = 10 * (attempt + 1)
                logger.warning(
                    "  Bedrock could not assume the Knowledge Base role yet "
                    f"(attempt {attempt + 1}/{max_retries}). Retrying in {wait_seconds}s..."
                )
                logger.warning(
                    "  If this persists, ensure the EC2/instance role has iam:PassRole "
                    f"for {knowledge_base_role_arn}"
                )
                time.sleep(wait_seconds)
                continue
            raise

    if response is None:
        raise Exception("Knowledge Base creation failed after retries")

    knowledge_base_id = response["knowledgeBase"]["knowledgeBaseId"]
    logger.info(f"✓ Knowledge Base created: {knowledge_base_id}")

    logger.info("  Waiting for Knowledge Base to be active...")
    while True:
        kb_response = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
        status = kb_response["knowledgeBase"]["status"]

        if status == "ACTIVE":
            logger.info("  Knowledge Base is now active")
            break
        if status == "FAILED":
            raise Exception("Knowledge Base creation failed")

        logger.debug(f"  Knowledge Base status: {status} (waiting...)")
        time.sleep(10)

    data_source_id = ensure_data_source(bedrock_agent_client, knowledge_base_id, s3_bucket_name)
    return knowledge_base_id, data_source_id


def create_agentcore_memory_role() -> str:
    """Create AgentCore Memory IAM role."""
    logger.info("[2/10] Creating AgentCore Memory IAM role")
    role_name = f"role-agentcore-memory-for-{project_name}-{region}"

    # Trust must include aws:SourceAccount / aws:SourceArn; CreateMemory rejects otherwise.
    # https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/long-term-configuring-custom-strategies.html
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "MemoryAssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{region}:{account_id}:*"
                        )
                    },
                },
            }
        ],
    }

    role_existed = False
    try:
        iam_client.get_role(RoleName=role_name)
        role_existed = True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in (
            "NoSuchEntity",
            "NoSuchEntityException",
        ):
            raise

    role_arn = create_iam_role(role_name, assume_role_policy)

    memory_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    "arn:aws:bedrock:*:*:inference-profile/*",
                ],
                "Condition": {
                    "StringEquals": {
                        "aws:ResourceAccount": account_id
                    }
                },
            }
        ],
    }
    attach_inline_policy(role_name, f"agentcore-memory-policy-for-{project_name}", memory_policy)

    # CreateMemory validates trust immediately after a brand-new role.
    if not role_existed:
        logger.info("  Waiting for IAM role trust policy to propagate...")
        time.sleep(10)
    else:
        logger.info("  Skipping IAM wait (memory role already exists)")

    return role_arn


USER_PREFERENCE_PROMPT = (
    "You are tasked with analyzing conversations to extract the user's preferences. You'll be analyzing two sets of data:\n"
    "<past_conversation>\n"
    "[Past conversations between the user and system will be placed here for context]\n"
    "</past_conversation>\n"
    "<current_conversation>\n"
    "[The current conversation between the user and system will be placed here]\n"
    "</current_conversation>\n"
    "Your job is to identify and categorize the user's preferences into two main types:\n"
    "- Explicit preferences: Directly stated preferences by the user.\n"
    "- Implicit preferences: Inferred from patterns, repeated inquiries, or contextual clues. Take a close look at user's request for implicit preferences.\n"
    "For explicit preference, extract only preference that the user has explicitly shared. Do not infer user's preference.\n"
    "For implicit preference, it is allowed to infer user's preference, but only the ones with strong signals, such as requesting something multiple times.\n"
    "Use Korean.\n"
)

SUMMARY_PROMPT = (
    "You will be given a text block and a list of summaries you previously generated when available.\n"
    "<task>\n"
    "- When the previously generated is not available, your goal is to summarize the given text block.\n"
    "- When there is existing summary, your goal is to extend summary by taking into account the given text block.\n"
    "- If there are queries/topics specified in the text block, your generated summary need to cover those queries/topics.\n"
    "- If there are instructions in the text block **guiding you how to generate summary**, you MUST follow them.\n"
    "</task>\n"
    "Use Korean.\n"
)

SEMANTIC_PROMPT = (
    "You are a long-term memory extraction agent supporting a lifelong learning system.\n"
    "Your task is to identify and extract meaningful information about the users from a given list of messages.\n"
    "Analyze the conversation and extract structured information about the user according to the schema below.\n"
    "Only include details that are explicitly stated or can be logically inferred from the conversation.\n"
    "- Extract information ONLY from the user messages. You should use assistant messages only as supporting context.\n"
    "- If the conversation contains no relevant or noteworthy information, return an empty list.\n"
    "- Do NOT extract anything from prior conversation history, even if provided. Use it solely for context.\n"
    "- Do NOT incorporate external knowledge.\n"
    "- Avoid duplicate extractions.\n"
    "Use Korean.\n"
)

SEMANTIC_CONSOLIDATION_PROMPT = (
    "You consolidate newly extracted facts with existing long-term semantic memories.\n"
    "- Merge duplicates; keep the most specific and recent facts.\n"
    "- Do not invent facts that were not extracted.\n"
    "- Prefer clear, atomic statements in Korean.\n"
    "Use Korean.\n"
)

MEMORY_EXTRACTION_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _shared_memory_strategies() -> list:
    """UserPreference + Summary + Semantic (one of each kind per memory_id)."""
    return [
        {
            "customMemoryStrategy": {
                "name": "UserPreference",
                "namespaces": ["/users/{actorId}/preferences"],
                "configuration": {
                    "userPreferenceOverride": {
                        "extraction": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": USER_PREFERENCE_PROMPT,
                        }
                    }
                },
            }
        },
        {
            "customMemoryStrategy": {
                "name": "Summary",
                "namespaces": ["/users/{actorId}/sessions/{sessionId}"],
                "configuration": {
                    "summaryOverride": {
                        "consolidation": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": SUMMARY_PROMPT,
                        }
                    }
                },
            }
        },
        {
            "customMemoryStrategy": {
                "name": "Semantic",
                "namespaces": ["/users/{actorId}/facts"],
                "configuration": {
                    "semanticOverride": {
                        "extraction": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": SEMANTIC_PROMPT,
                        },
                        "consolidation": {
                            "modelId": MEMORY_EXTRACTION_MODEL_ID,
                            "appendToPrompt": SEMANTIC_CONSOLIDATION_PROMPT,
                        },
                    }
                },
            }
        },
    ]


def create_agentcore_memory(role_arn: str, user_id: str = "installer") -> str:
    """
    Create AgentCore Memory with shared UserPreference / Summary / Semantic strategies.

    user_id is unused for strategy naming — kept for call-site compatibility.
    User isolation uses {actorId}/{sessionId} namespace templates from CreateEvent.
    """
    logger.info("[2/10] Creating AgentCore Memory")

    if MemoryClient is None:
        raise ImportError(
            "bedrock-agentcore is required to create AgentCore Memory. "
            "Install with: pip install bedrock-agentcore"
        )

    memory_client = MemoryClient(region_name=region)
    memory_name = project_name.replace("-", "_")

    memories = memory_client.list_memories()
    for memory in memories:
        if memory.get("id", "").split("-")[0] == memory_name:
            memory_id = memory.get("id")
            logger.info(f"  Memory already exists: {memory_id}")
            return memory_id

    strategies = _shared_memory_strategies()
    result = memory_client.create_memory_and_wait(
        name=memory_name,
        description=f"Memory for {project_name}",
        event_expiry_days=365,
        strategies=strategies,
        memory_execution_role_arn=role_arn,
    )
    memory_id = result.get("id")
    names = [s["customMemoryStrategy"]["name"] for s in strategies]
    logger.info(f"  ✓ Memory created: {memory_id} (strategies={names})")
    return memory_id


def _agentcore_websearch_tool_arn() -> str:
    return (
        f"arn:aws:bedrock-agentcore:{AGENTCORE_GATEWAY_REGION}:"
        f"aws:tool/web-search.v1"
    )


def _list_all_agentcore_gateways() -> List[Dict]:
    gateways: List[Dict] = []
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = agentcore_control_client.list_gateways(**kwargs)
        gateways.extend(response.get("items", []))
        next_token = response.get("nextToken")
        if not next_token:
            break
    return gateways


def _list_all_agentcore_gateway_targets(gateway_id: str) -> List[Dict]:
    targets: List[Dict] = []
    next_token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id}
        if next_token:
            kwargs["nextToken"] = next_token
        response = agentcore_control_client.list_gateway_targets(**kwargs)
        targets.extend(response.get("items", []))
        next_token = response.get("nextToken")
        if not next_token:
            break
    return targets


def wait_for_agentcore_gateway_ready(gateway_id: str, timeout_seconds: int = 600) -> Dict:
    """Wait until an AgentCore gateway reaches READY status."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        gateway = agentcore_control_client.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway.get("status", "")
        if status == "READY":
            logger.info(f"  AgentCore gateway is ready: {gateway_id}")
            return gateway
        if status in ("FAILED", "DELETING", "DELETE_UNSUCCESSFUL", "UPDATE_UNSUCCESSFUL"):
            raise RuntimeError(
                f"AgentCore gateway {gateway_id} entered terminal status: {status}"
            )
        logger.info(f"  Waiting for AgentCore gateway ({gateway_id}) status: {status}")
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for AgentCore gateway {gateway_id} to become READY")


def create_agentcore_websearch_gateway_role() -> str:
    """Create IAM service role for the AgentCore Web Search gateway."""
    logger.info("[2/10] Creating AgentCore Web Search gateway IAM role")
    role_name = f"role-agentcore-gateway-websearch-for-{project_name}"

    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "GatewayAssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{AGENTCORE_GATEWAY_REGION}:"
                            f"{account_id}:gateway/{AGENTCORE_WEBSEARCH_GATEWAY_NAME}-*"
                        )
                    },
                },
            }
        ],
    }
    role_arn = create_iam_role(role_name, assume_role_policy)

    gateway_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "InvokeGateway",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                "Resource": [
                    (
                        f"arn:aws:bedrock-agentcore:{AGENTCORE_GATEWAY_REGION}:"
                        f"{account_id}:gateway/*"
                    )
                ],
            },
            {
                "Sid": "InvokeWebSearchTool",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeWebSearch"],
                "Resource": [_agentcore_websearch_tool_arn()],
            },
        ],
    }
    attach_inline_policy(
        role_name,
        f"agentcore-gateway-websearch-policy-for-{project_name}",
        gateway_policy,
    )
    return role_arn


def _agentcore_mcp_supports_connector(client) -> bool:
    """Return True when the local botocore model includes the MCP connector target."""
    try:
        shape = client.meta.service_model.shape_for("McpTargetConfiguration")
        return "connector" in shape.members
    except Exception:
        return False


def ensure_boto3_supports_agentcore_connector() -> None:
    """Upgrade boto3/botocore when the SDK model lacks web-search connector support."""
    import botocore

    if _agentcore_mcp_supports_connector(agentcore_control_client):
        return

    current = getattr(botocore, "__version__", "unknown")
    logger.warning(
        "Local boto3/botocore (%s) is missing AgentCore MCP connector support "
        "required for the web-search gateway. Upgrading to >= %s...",
        current,
        MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR,
    )
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                f"boto3>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}",
                f"botocore>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}",
            ]
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"boto3/botocore upgrade failed (current botocore {current}). "
            "Install manually: "
            f"pip install --upgrade 'boto3>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}' "
            f"'botocore>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}'"
        ) from e

    logger.info("boto3 upgraded; restarting installer with updated SDK...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _websearch_gateway_target_configuration() -> Dict:
    return {
        "mcp": {
            "connector": {
                "source": {
                    "connectorId": "web-search",
                },
                "configurations": [
                    {
                        "name": "WebSearch",
                        "parameterValues": {},
                    }
                ],
            }
        }
    }


def _ensure_websearch_gateway_target(gateway_id: str) -> str:
    """Create the managed web-search connector target if it does not exist."""
    for target in _list_all_agentcore_gateway_targets(gateway_id):
        if target.get("name") == AGENTCORE_WEBSEARCH_TARGET_NAME:
            target_id = target["targetId"]
            logger.warning(
                f"  AgentCore websearch target already exists: {target_id}"
            )
            return target_id

    if not _agentcore_mcp_supports_connector(agentcore_control_client):
        raise RuntimeError(
            "boto3/botocore is too old for AgentCore web-search connector targets. "
            f"Run: pip install --upgrade 'boto3>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}' "
            f"'botocore>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}'"
        )

    logger.info("  Creating AgentCore websearch gateway target")
    create_kwargs = {
        "gatewayIdentifier": gateway_id,
        "name": AGENTCORE_WEBSEARCH_TARGET_NAME,
        "description": f"Managed Web Search connector for {project_name}",
        "targetConfiguration": _websearch_gateway_target_configuration(),
        "credentialProviderConfigurations": [
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    }
    try:
        response = agentcore_control_client.create_gateway_target(**create_kwargs)
    except ParamValidationError as e:
        raise RuntimeError(
            "Failed to create AgentCore web-search gateway target because the local "
            "boto3/botocore service model is outdated (missing targetConfiguration.mcp.connector). "
            f"Upgrade with: pip install --upgrade 'boto3>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}' "
            f"'botocore>={MIN_BOTO3_VERSION_FOR_AGENTCORE_CONNECTOR}'"
        ) from e
    target_id = response["targetId"]
    logger.info(f"  ✓ AgentCore websearch target created: {target_id}")

    try:
        agentcore_control_client.synchronize_gateway_targets(
            gatewayIdentifier=gateway_id,
            targetIdList=[target_id],
        )
    except ClientError as e:
        logger.warning(f"  Could not synchronize gateway target immediately: {e}")

    return target_id


def get_or_create_agentcore_websearch_gateway(gateway_service_role_arn: str) -> Dict[str, str]:
    """Create gateway-websearch with the managed web-search connector in us-east-1."""
    logger.info("[2/10] Creating AgentCore Web Search gateway")

    gateway_id = None
    for gateway in _list_all_agentcore_gateways():
        if gateway.get("name") == AGENTCORE_WEBSEARCH_GATEWAY_NAME:
            gateway_id = gateway["gatewayId"]
            logger.warning(
                f"  AgentCore gateway already exists: "
                f"{AGENTCORE_WEBSEARCH_GATEWAY_NAME} ({gateway_id})"
            )
            break

    if not gateway_id:
        response = agentcore_control_client.create_gateway(
            name=AGENTCORE_WEBSEARCH_GATEWAY_NAME,
            description=f"AgentCore Web Search gateway for {project_name}",
            roleArn=gateway_service_role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            tags={"project": project_name},
        )
        gateway_id = response["gatewayId"]
        logger.info(f"  ✓ AgentCore gateway created: {gateway_id}")
        wait_for_agentcore_gateway_ready(gateway_id)

    gateway = wait_for_agentcore_gateway_ready(gateway_id)
    target_id = _ensure_websearch_gateway_target(gateway_id)
    gateway_url = gateway.get("gatewayUrl", "").rstrip("/")

    return {
        "gateway_id": gateway_id,
        "gateway_name": AGENTCORE_WEBSEARCH_GATEWAY_NAME,
        "gateway_region": AGENTCORE_GATEWAY_REGION,
        "gateway_url": gateway_url,
        "gateway_arn": gateway.get("gatewayArn", ""),
        "gateway_service_role_arn": gateway_service_role_arn,
        "target_id": target_id,
    }


def _apply_websearch_gateway_config(
    env: Dict[str, str],
    agentcore_websearch_gateway_info: Optional[Dict[str, str]] = None,
) -> None:
    """Add AgentCore websearch gateway settings to an environment/config dict."""
    if not agentcore_websearch_gateway_info:
        return
    env["agentcore_websearch_gateway_name"] = agentcore_websearch_gateway_info.get(
        "gateway_name", AGENTCORE_WEBSEARCH_GATEWAY_NAME
    )
    env["agentcore_websearch_gateway_region"] = agentcore_websearch_gateway_info.get(
        "gateway_region", AGENTCORE_GATEWAY_REGION
    )
    env["agentcore_websearch_gateway_id"] = agentcore_websearch_gateway_info.get(
        "gateway_id", ""
    )
    env["agentcore_websearch_gateway_url"] = agentcore_websearch_gateway_info.get(
        "gateway_url", ""
    )
    env["agentcore_websearch_gateway_role"] = agentcore_websearch_gateway_info.get(
        "gateway_service_role_arn", ""
    )


def _cloudfront_distribution_comment() -> str:
    return f"CloudFront-for-{project_name}"


def _load_application_sharing_domain() -> Optional[str]:
    """Return CloudFront domain from application/config.json if configured."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "application", "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        sharing_url = (data.get("sharing_url") or "").strip()
        if not sharing_url:
            return None
        parsed = urlparse(sharing_url)
        return parsed.netloc or sharing_url.replace("https://", "").replace("http://", "").strip("/")
    except (OSError, json.JSONDecodeError):
        return None


def _list_all_cloudfront_distributions() -> List[Dict]:
    """List all CloudFront distributions (handles pagination)."""
    items: List[Dict] = []
    marker: Optional[str] = None
    while True:
        kwargs = {"Marker": marker} if marker else {}
        response = cloudfront_client.list_distributions(**kwargs)
        dist_list = response.get("DistributionList") or {}
        items.extend(dist_list.get("Items") or [])
        if not dist_list.get("IsTruncated"):
            break
        marker = dist_list.get("NextMarker")
    return items


def _find_existing_cloudfront_distribution() -> Optional[Dict]:
    """Find an existing project CloudFront distribution to reuse."""
    comment = _cloudfront_distribution_comment()
    preferred_domain = _load_application_sharing_domain()

    try:
        all_dists = _list_all_cloudfront_distributions()
    except Exception as e:
        logger.warning(f"Could not list CloudFront distributions: {e}")
        return None

    matches = [d for d in all_dists if comment in (d.get("Comment") or "")]
    if not matches:
        return None

    if len(matches) > 1:
        domains = ", ".join(d.get("DomainName", "?") for d in matches)
        logger.warning(
            f"Found {len(matches)} CloudFront distributions with comment '{comment}': {domains}"
        )
        logger.warning(
            "  Reusing one distribution only; disable or delete unused duplicates in the AWS console."
        )

    if preferred_domain:
        for dist in matches:
            if dist.get("DomainName") == preferred_domain:
                logger.info(f"  Reusing CloudFront from application/config.json sharing_url: {preferred_domain}")
                return dist
        logger.warning(
            f"  sharing_url domain '{preferred_domain}' not found among matching distributions; "
            "falling back to first enabled match."
        )

    enabled = [d for d in matches if d.get("Enabled")]
    return enabled[0] if enabled else matches[0]


def _reuse_cloudfront_distribution(
    dist: Dict, origin_header_value: str
) -> Dict[str, str]:
    """Reuse an existing CloudFront distribution and ensure required cache behaviors."""
    dist_id = dist["Id"]
    domain = dist["DomainName"]
    logger.info(f"Reusing existing CloudFront distribution: {domain} (Id: {dist_id})")

    if not dist.get("Enabled"):
        logger.warning(f"CloudFront distribution exists but is disabled: {domain}")
        logger.info("  Enabling existing CloudFront distribution...")
        dist_config_response = cloudfront_client.get_distribution_config(Id=dist_id)
        dist_config = dist_config_response["DistributionConfig"]
        etag = dist_config_response["ETag"]
        dist_config["Enabled"] = True
        cloudfront_client.update_distribution(
            Id=dist_id,
            DistributionConfig=dist_config,
            IfMatch=etag,
        )
        logger.info(f"  ✓ Enabled CloudFront distribution: {domain}")

    signing = get_or_create_cloudfront_signing_material(rotate=False)
    key_group_id = signing.get("key_group_id") or ""

    try:
        ensure_cloudfront_s3_signed_cookies(dist_id, key_group_id)
    except Exception as e:
        logger.warning(
            f"Could not update CloudFront signed-cookie behaviors "
            f"(reusing distribution anyway): {e}"
        )

    try:
        ensure_cloudfront_security_headers(dist_id)
    except Exception as e:
        logger.warning(
            f"Could not attach CloudFront security response headers "
            f"(reusing distribution anyway): {e}"
        )

    try:
        oai_id = _find_s3_oai_id_from_distribution(dist_id)
        if oai_id:
            ensure_cloudfront_oai_bucket_policy(bucket_name, oai_id)
        else:
            logger.warning("  Could not find S3 OAI on CloudFront distribution; skip bucket policy")
    except Exception as e:
        logger.warning(
            f"Could not tighten CloudFront OAI S3 bucket policy "
            f"(reusing distribution anyway): {e}"
        )

    try:
        _ensure_cloudfront_alb_origin_config(dist_id, origin_header_value)
    except Exception as e:
        logger.warning(
            f"Could not update CloudFront ALB origin config (reusing distribution anyway): {e}"
        )

    return {
        "id": dist_id,
        "domain": domain,
        "key_pair_id": signing.get("public_key_id") or "",
        "key_group_id": key_group_id,
        "private_key_pem": signing.get("private_key_pem") or "",
    }

def ensure_alb_idle_timeout(
    alb_arn: str, timeout_seconds: int = ALB_IDLE_TIMEOUT_SECONDS
) -> None:
    """Raise ALB idle timeout so long SSE streams are not cut at 60s."""
    try:
        elbv2_client.modify_load_balancer_attributes(
            LoadBalancerArn=alb_arn,
            Attributes=[
                {
                    "Key": "idle_timeout.timeout_seconds",
                    "Value": str(timeout_seconds),
                }
            ],
        )
        logger.info(f"  ✓ ALB idle timeout set to {timeout_seconds}s")
    except ClientError as e:
        logger.warning(f"Could not set ALB idle timeout: {e}")


def _cloudfront_alb_custom_origin_config() -> Dict[str, object]:
    return {
        "HTTPPort": 80,
        "HTTPSPort": 443,
        "OriginProtocolPolicy": "http-only",
        "OriginReadTimeout": SSE_ORIGIN_READ_TIMEOUT_SECONDS,
        "OriginKeepaliveTimeout": 5,
    }


def _ensure_cloudfront_alb_origin_config(
    dist_id: str,
    origin_header_value: str,
    timeout_seconds: int = SSE_ORIGIN_READ_TIMEOUT_SECONDS,
) -> None:
    """Ensure CloudFront ALB origin sends the secret header and supports long SSE reads."""
    dist_config_response = cloudfront_client.get_distribution_config(Id=dist_id)
    dist_config = dist_config_response["DistributionConfig"]
    etag = dist_config_response["ETag"]

    alb_origin_id = f"alb-{project_name}"
    origins = dist_config.get("Origins", {}).get("Items", [])
    updated = False
    for origin in origins:
        if origin.get("Id") != alb_origin_id:
            continue
        custom = origin.setdefault("CustomOriginConfig", {})
        if custom.get("OriginReadTimeout") != timeout_seconds:
            custom["OriginReadTimeout"] = timeout_seconds
            updated = True
        custom.setdefault("OriginKeepaliveTimeout", 5)

        desired_headers = _cloudfront_alb_origin_custom_headers(origin_header_value)
        current_headers = origin.get("CustomHeaders") or {"Quantity": 0, "Items": []}
        current_items = current_headers.get("Items") or []
        header_ok = any(
            item.get("HeaderName") == custom_header_name
            and item.get("HeaderValue") == origin_header_value
            for item in current_items
        )
        if not header_ok or current_headers.get("Quantity") != 1:
            origin["CustomHeaders"] = desired_headers
            updated = True
        break
    else:
        logger.warning(
            f"  CloudFront ALB origin {alb_origin_id} not found; skipping origin config update"
        )
        return

    if not updated:
        logger.info(
            f"  CloudFront ALB origin header/timeout already configured "
            f"(timeout={timeout_seconds}s)"
        )
        return

    cloudfront_client.update_distribution(
        Id=dist_id,
        DistributionConfig=dist_config,
        IfMatch=etag,
    )
    logger.info(
        f"  ✓ CloudFront ALB origin updated "
        f"(custom header + read timeout {timeout_seconds}s)"
    )
    logger.warning(
        "  Note: CloudFront origin changes may take 15-20 minutes to deploy"
    )


def _ensure_cloudfront_alb_origin_timeouts(
    dist_id: str, timeout_seconds: int = SSE_ORIGIN_READ_TIMEOUT_SECONDS
) -> None:
    """Backward-compatible wrapper; prefer _ensure_cloudfront_alb_origin_config."""
    header_value = get_or_create_alb_origin_header(rotate=False)
    _ensure_cloudfront_alb_origin_config(dist_id, header_value, timeout_seconds)




def _generate_cloudfront_rsa_keypair() -> Tuple[str, str]:
    """Return (private_pem, public_pem) for CloudFront signed cookies.

    Prefers the ``cryptography`` package; falls back to OpenSSL CLI so the
    installer host need not have app ``requirements.txt`` installed.
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("utf-8")
        )
        return private_pem, public_pem
    except ImportError:
        pass

    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "CloudFront signing key generation requires the 'cryptography' package "
            "or the 'openssl' CLI. Install with: pip install cryptography"
        )

    import tempfile

    with tempfile.TemporaryDirectory(prefix="cf-signing-") as tmp:
        key_path = os.path.join(tmp, "private.pem")
        pub_path = os.path.join(tmp, "public.pem")
        gen = subprocess.run(
            [openssl, "genrsa", "-out", key_path, "2048"],
            capture_output=True,
            text=True,
            check=False,
        )
        if gen.returncode != 0:
            raise RuntimeError(
                f"openssl genrsa failed (exit {gen.returncode}): {gen.stderr.strip()}"
            )
        pub = subprocess.run(
            [openssl, "rsa", "-in", key_path, "-pubout", "-out", pub_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if pub.returncode != 0:
            raise RuntimeError(
                f"openssl rsa -pubout failed (exit {pub.returncode}): {pub.stderr.strip()}"
            )
        with open(key_path, "r", encoding="utf-8") as f:
            private_pem = f.read()
        with open(pub_path, "r", encoding="utf-8") as f:
            public_pem = f.read()
    return private_pem, public_pem


def _find_cloudfront_public_key_id(name: str) -> Optional[str]:
    marker = None
    while True:
        kwargs = {"MaxItems": "100"}
        if marker:
            kwargs["Marker"] = marker
        response = cloudfront_client.list_public_keys(**kwargs)
        listing = response.get("PublicKeyList") or {}
        for item in listing.get("Items") or []:
            if item.get("Name") == name:
                return item.get("Id")
        if not listing.get("IsTruncated"):
            return None
        marker = listing.get("NextMarker")


def _find_cloudfront_key_group_id(name: str) -> Optional[str]:
    marker = None
    while True:
        kwargs = {"MaxItems": "100"}
        if marker:
            kwargs["Marker"] = marker
        response = cloudfront_client.list_key_groups(**kwargs)
        listing = response.get("KeyGroupList") or {}
        for summary in listing.get("Items") or []:
            kg = summary.get("KeyGroup") or summary
            config = kg.get("KeyGroupConfig") or {}
            if config.get("Name") == name or kg.get("Name") == name:
                return kg.get("Id")
        if not listing.get("IsTruncated"):
            return None
        marker = listing.get("NextMarker")


def get_or_create_cloudfront_signing_material(*, rotate: bool = False) -> Dict[str, str]:
    """
    Ensure CloudFront RSA signing material exists.

    Stores private PEM + public_key_id + key_group_id in Secrets Manager.
    Creates CloudFront PublicKey + KeyGroup when missing.
    """
    secret_name = CLOUDFRONT_SIGNING_KEY_SECRET_NAME
    public_key_name = f"{project_name}-signing-key"
    key_group_name = f"{project_name}-signing-key-group"

    material: Dict[str, str] = {}
    try:
        existing = secretsmanager_client.get_secret_value(SecretId=secret_name)
        raw = (existing.get("SecretString") or "").strip()
        if raw.startswith("{"):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                material = {
                    "private_key_pem": (parsed.get("private_key_pem") or "").strip(),
                    "public_key_pem": (parsed.get("public_key_pem") or "").strip(),
                    "public_key_id": (parsed.get("public_key_id") or "").strip(),
                    "key_group_id": (parsed.get("key_group_id") or "").strip(),
                }
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    if rotate or not material.get("private_key_pem") or not material.get("public_key_pem"):
        private_pem, public_pem = _generate_cloudfront_rsa_keypair()
        material["private_key_pem"] = private_pem
        material["public_key_pem"] = public_pem
        material["public_key_id"] = ""
        material["key_group_id"] = ""
        logger.info("  Generated new CloudFront RSA signing key pair")

    public_key_id = material.get("public_key_id") or ""
    if not public_key_id:
        public_key_id = _find_cloudfront_public_key_id(public_key_name) or ""
    if not public_key_id:
        response = cloudfront_client.create_public_key(
            PublicKeyConfig={
                "CallerReference": f"{project_name}-cf-pk-{int(time.time())}",
                "Name": public_key_name,
                "EncodedKey": material["public_key_pem"],
                "Comment": f"Signed cookies public key for {project_name}",
            }
        )
        public_key_id = response["PublicKey"]["Id"]
        logger.info(f"  ✓ Created CloudFront public key: {public_key_id}")
    else:
        logger.info(f"  ✓ Reusing CloudFront public key: {public_key_id}")
    material["public_key_id"] = public_key_id

    key_group_id = material.get("key_group_id") or ""
    if not key_group_id:
        key_group_id = _find_cloudfront_key_group_id(key_group_name) or ""
    if not key_group_id:
        response = cloudfront_client.create_key_group(
            KeyGroupConfig={
                "Name": key_group_name,
                "Items": [public_key_id],
                "Comment": f"Signed cookies key group for {project_name}",
            }
        )
        key_group_id = response["KeyGroup"]["Id"]
        logger.info(f"  ✓ Created CloudFront key group: {key_group_id}")
    else:
        try:
            kg = cloudfront_client.get_key_group(Id=key_group_id)
            etag = kg["ETag"]
            config = kg["KeyGroup"]["KeyGroupConfig"]
            items = list(config.get("Items") or [])
            if public_key_id not in items:
                config["Items"] = [public_key_id]
                cloudfront_client.update_key_group(
                    Id=key_group_id,
                    KeyGroupConfig=config,
                    IfMatch=etag,
                )
                logger.info(f"  ✓ Updated CloudFront key group items: {key_group_id}")
            else:
                logger.info(f"  ✓ Reusing CloudFront key group: {key_group_id}")
        except ClientError as e:
            logger.warning(f"  Could not verify key group {key_group_id}: {e}")
    material["key_group_id"] = key_group_id

    secret_body = json.dumps(
        {
            "private_key_pem": material["private_key_pem"],
            "public_key_pem": material["public_key_pem"],
            "public_key_id": material["public_key_id"],
            "key_group_id": material["key_group_id"],
        }
    )
    try:
        secretsmanager_client.put_secret_value(
            SecretId=secret_name,
            SecretString=secret_body,
        )
        logger.info(f"  ✓ Updated CloudFront signing secret: {secret_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        secretsmanager_client.create_secret(
            Name=secret_name,
            Description=f"CloudFront signed-cookie RSA key material for {project_name}",
            SecretString=secret_body,
            Tags=[
                {"Key": "Name", "Value": secret_name},
                {"Key": "Project", "Value": project_name},
            ],
        )
        logger.info(f"  ✓ Created CloudFront signing secret: {secret_name}")

    return material




def _cloudfront_s3_cache_behavior(
    path_pattern: str,
    s3_origin_id: str,
    key_group_id: Optional[str] = None,
) -> Dict[str, object]:
    """CloudFront cache behavior routing a path prefix to the S3 origin."""
    behavior: Dict[str, object] = {
        "PathPattern": path_pattern,
        "TargetOriginId": s3_origin_id,
        "ViewerProtocolPolicy": "redirect-to-https",
        "AllowedMethods": {
            "Quantity": 2,
            "Items": ["GET", "HEAD"],
            "CachedMethods": {
                "Quantity": 2,
                "Items": ["GET", "HEAD"],
            },
        },
        "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        "ResponseHeadersPolicyId": cloudfront_response_headers_policy_id(),
        "Compress": True,
    }
    if key_group_id:
        behavior["TrustedKeyGroups"] = {
            "Enabled": True,
            "Quantity": 1,
            "Items": [key_group_id],
        }
        behavior["TrustedSigners"] = {
            "Enabled": False,
            "Quantity": 0,
        }
    return behavior


def _behavior_has_trusted_key_group(behavior: Dict, key_group_id: str) -> bool:
    tkg = behavior.get("TrustedKeyGroups") or {}
    if not tkg.get("Enabled"):
        return False
    return key_group_id in (tkg.get("Items") or [])


def ensure_cloudfront_s3_signed_cookies(dist_id: str, key_group_id: str) -> None:
    """Ensure /images|/docs|/artifacts behaviors exist and require TrustedKeyGroups."""
    if not key_group_id:
        raise ValueError("key_group_id is required for CloudFront signed cookies")

    dist_config_response = cloudfront_client.get_distribution_config(Id=dist_id)
    dist_config = dist_config_response["DistributionConfig"]
    etag = dist_config_response["ETag"]

    cache_behaviors = dist_config.get("CacheBehaviors") or {"Quantity": 0, "Items": []}
    items = list(cache_behaviors.get("Items") or [])
    s3_origin_id = f"s3-{project_name}"
    changed = False

    by_path = {item.get("PathPattern"): item for item in items}
    for path_pattern in CLOUDFRONT_S3_SIGNED_PATHS:
        existing = by_path.get(path_pattern)
        if existing is None:
            items.append(
                _cloudfront_s3_cache_behavior(path_pattern, s3_origin_id, key_group_id)
            )
            changed = True
            logger.info(f"  + CloudFront behavior {path_pattern} (signed)")
            continue
        if not _behavior_has_trusted_key_group(existing, key_group_id):
            existing["TrustedKeyGroups"] = {
                "Enabled": True,
                "Quantity": 1,
                "Items": [key_group_id],
            }
            existing["TrustedSigners"] = {
                "Enabled": False,
                "Quantity": 0,
            }
            changed = True
            logger.info(f"  ✓ CloudFront behavior {path_pattern}: TrustedKeyGroups enabled")

    if not changed:
        logger.info("  ✓ CloudFront S3 behaviors already require signed cookies")
        return

    dist_config["CacheBehaviors"] = {"Quantity": len(items), "Items": items}
    cloudfront_client.update_distribution(
        Id=dist_id,
        DistributionConfig=dist_config,
        IfMatch=etag,
    )
    logger.info("  ✓ Updated CloudFront distribution for signed cookies")
    logger.warning("  Note: CloudFront behavior changes may take 15-20 minutes to deploy")



def get_or_create_cloudfront_response_headers_policy() -> str:
    """Security headers + remove origin Server (hides uvicorn) for viewer responses.

    Removing Server makes CloudFront emit ``Server: CloudFront`` instead of
    passing through ``Server: uvicorn``. CSP stays in app middleware.
    """
    global _cloudfront_response_headers_policy_id
    if _cloudfront_response_headers_policy_id:
        return _cloudfront_response_headers_policy_id

    policy_name = f"{project_name}-security-headers"
    marker: Optional[str] = None
    while True:
        kwargs: Dict[str, object] = {"Type": "custom", "MaxItems": "100"}
        if marker:
            kwargs["Marker"] = marker
        resp = cloudfront_client.list_response_headers_policies(**kwargs)
        policy_list = resp.get("ResponseHeadersPolicyList") or {}
        for item in policy_list.get("Items") or []:
            policy = item.get("ResponseHeadersPolicy") or {}
            cfg = policy.get("ResponseHeadersPolicyConfig") or {}
            if cfg.get("Name") == policy_name and policy.get("Id"):
                _cloudfront_response_headers_policy_id = policy["Id"]
                logger.info(
                    "  ✓ Using existing CloudFront response headers policy: %s",
                    _cloudfront_response_headers_policy_id,
                )
                return _cloudfront_response_headers_policy_id
        if not policy_list.get("IsTruncated"):
            break
        marker = policy_list.get("NextMarker")
        if not marker:
            break

    config = {
        "Name": policy_name,
        "Comment": (
            f"Security headers for {project_name}; strip origin Server header"
        ),
        "SecurityHeadersConfig": {
            "XSSProtection": {
                "Override": True,
                "Protection": True,
                "ModeBlock": True,
            },
            "FrameOptions": {"Override": True, "FrameOption": "DENY"},
            "ReferrerPolicy": {
                "Override": True,
                "ReferrerPolicy": "strict-origin-when-cross-origin",
            },
            "ContentTypeOptions": {"Override": True},
            "StrictTransportSecurity": {
                "Override": True,
                "AccessControlMaxAgeSec": 31536000,
                "IncludeSubdomains": True,
                "Preload": False,
            },
        },
        "RemoveHeadersConfig": {
            "Quantity": 2,
            "Items": [
                {"Header": "Server"},
                {"Header": "X-Powered-By"},
            ],
        },
    }
    try:
        created = cloudfront_client.create_response_headers_policy(
            ResponseHeadersPolicyConfig=config
        )
        _cloudfront_response_headers_policy_id = created["ResponseHeadersPolicy"]["Id"]
        logger.info(
            "  ✓ Created CloudFront response headers policy: %s",
            _cloudfront_response_headers_policy_id,
        )
        return _cloudfront_response_headers_policy_id
    except ClientError as e:
        logger.warning("  Response headers policy create failed; re-listing: %s", e)
        marker = None
        while True:
            kwargs = {"Type": "custom", "MaxItems": "100"}
            if marker:
                kwargs["Marker"] = marker
            resp = cloudfront_client.list_response_headers_policies(**kwargs)
            policy_list = resp.get("ResponseHeadersPolicyList") or {}
            for item in policy_list.get("Items") or []:
                policy = item.get("ResponseHeadersPolicy") or {}
                cfg = policy.get("ResponseHeadersPolicyConfig") or {}
                if cfg.get("Name") == policy_name and policy.get("Id"):
                    _cloudfront_response_headers_policy_id = policy["Id"]
                    return _cloudfront_response_headers_policy_id
            if not policy_list.get("IsTruncated"):
                break
            marker = policy_list.get("NextMarker")
            if not marker:
                break
        raise


def cloudfront_response_headers_policy_id() -> str:
    """Cached custom ResponseHeadersPolicy id (create on first use)."""
    return get_or_create_cloudfront_response_headers_policy()


def _behavior_has_security_headers(behavior: Dict, policy_id: str) -> bool:
    return behavior.get("ResponseHeadersPolicyId") == policy_id


def ensure_cloudfront_security_headers(dist_id: str) -> None:
    """Attach custom security ResponseHeadersPolicy to default + S3 cache behaviors."""
    policy_id = cloudfront_response_headers_policy_id()
    dist_config_response = cloudfront_client.get_distribution_config(Id=dist_id)
    dist_config = dist_config_response["DistributionConfig"]
    etag = dist_config_response["ETag"]
    changed = False

    default_behavior = dist_config.get("DefaultCacheBehavior") or {}
    if not _behavior_has_security_headers(default_behavior, policy_id):
        default_behavior["ResponseHeadersPolicyId"] = policy_id
        dist_config["DefaultCacheBehavior"] = default_behavior
        changed = True
        logger.info("  ✓ CloudFront default behavior: security response headers")

    cache_behaviors = dist_config.get("CacheBehaviors") or {"Quantity": 0, "Items": []}
    items = list(cache_behaviors.get("Items") or [])
    for item in items:
        if _behavior_has_security_headers(item, policy_id):
            continue
        item["ResponseHeadersPolicyId"] = policy_id
        changed = True
        logger.info(
            "  ✓ CloudFront behavior %s: security response headers",
            item.get("PathPattern"),
        )

    if not changed:
        logger.info("  ✓ CloudFront security response headers already attached")
        return

    dist_config["CacheBehaviors"] = {"Quantity": len(items), "Items": items}
    cloudfront_client.update_distribution(
        Id=dist_id,
        DistributionConfig=dist_config,
        IfMatch=etag,
    )
    logger.info("  ✓ Updated CloudFront distribution for security response headers")
    logger.warning("  Note: CloudFront behavior changes may take 15-20 minutes to deploy")


def _ensure_cloudfront_s3_path_behavior(
    dist_id: str,
    path_pattern: str,
    s3_origin_id: str,
    key_group_id: Optional[str] = None,
) -> None:
    """Add or update an S3 cache behavior on an existing CloudFront distribution."""
    dist_config_response = cloudfront_client.get_distribution_config(Id=dist_id)
    dist_config = dist_config_response["DistributionConfig"]
    etag = dist_config_response["ETag"]

    cache_behaviors = dist_config.get("CacheBehaviors") or {"Quantity": 0, "Items": []}
    items = list(cache_behaviors.get("Items") or [])

    for item in items:
        if item.get("PathPattern") != path_pattern:
            continue
        if key_group_id and not _behavior_has_trusted_key_group(item, key_group_id):
            item["TrustedKeyGroups"] = {
                "Enabled": True,
                "Quantity": 1,
                "Items": [key_group_id],
            }
            item["TrustedSigners"] = {"Enabled": False, "Quantity": 0}
            dist_config["CacheBehaviors"] = {"Quantity": len(items), "Items": items}
            cloudfront_client.update_distribution(
                Id=dist_id,
                DistributionConfig=dist_config,
                IfMatch=etag,
            )
            logger.info(f"  ✓ Updated CloudFront behavior for signed cookies: {path_pattern}")
            return
        logger.info(f"  CloudFront behavior already exists: {path_pattern}")
        return

    items.append(_cloudfront_s3_cache_behavior(path_pattern, s3_origin_id, key_group_id))
    dist_config["CacheBehaviors"] = {"Quantity": len(items), "Items": items}

    cloudfront_client.update_distribution(
        Id=dist_id,
        DistributionConfig=dist_config,
        IfMatch=etag,
    )
    logger.info(f"  ✓ Added CloudFront behavior: {path_pattern} -> {s3_origin_id}")
    logger.warning("  Note: CloudFront behavior changes may take 15-20 minutes to deploy")


def create_cloudfront_distribution(
    alb_info: Dict[str, str],
    s3_bucket_name: str,
    origin_header_value: str,
) -> Dict[str, str]:
    """Create CloudFront distribution with hybrid ALB + S3 origins."""
    logger.info("[7/10] Creating CloudFront distribution (ALB + S3 hybrid)")

    signing = get_or_create_cloudfront_signing_material(rotate=False)
    key_group_id = signing.get("key_group_id") or ""

    existing = _find_existing_cloudfront_distribution()
    if existing:
        return _reuse_cloudfront_distribution(existing, origin_header_value)
    
    # Check for existing Origin Access Identity or create new one (needed before creating distribution)
    logger.info("  Checking for existing Origin Access Identity for S3...")
    oai_id = None
    oai_canonical_user_id = None
    
    try:
        # Check existing OAIs
        oai_list = cloudfront_client.list_cloud_front_origin_access_identities()
        for oai in oai_list.get("CloudFrontOriginAccessIdentityList", {}).get("Items", []):
            if f"OAI for {project_name} S3 bucket" in oai.get("Comment", ""):
                oai_id = oai["Id"]
                oai_canonical_user_id = oai["S3CanonicalUserId"]
                logger.info(f"  ✓ Using existing Origin Access Identity: {oai_id}")
                break
        
        # Create new OAI if none exists
        if not oai_id:
            logger.info("  Creating new Origin Access Identity for S3...")
            oai_response = cloudfront_client.create_cloud_front_origin_access_identity(
                CloudFrontOriginAccessIdentityConfig={
                    "CallerReference": f"{project_name}-s3-oai-{int(time.time())}",
                    "Comment": f"OAI for {project_name} S3 bucket"
                }
            )
            oai_id = oai_response["CloudFrontOriginAccessIdentity"]["Id"]
            oai_canonical_user_id = oai_response["CloudFrontOriginAccessIdentity"]["S3CanonicalUserId"]
            logger.info(f"  ✓ Created Origin Access Identity: {oai_id}")
            
    except ClientError as e:
        logger.error(f"Failed to handle Origin Access Identity: {e}")
        raise
    
    # Update S3 bucket policy to allow CloudFront access (prefix-scoped)
    logger.info("  Updating S3 bucket policy for CloudFront access...")
    try:
        # Wait for OAI to propagate before applying bucket policy
        logger.info("  Waiting for OAI to propagate...")
        time.sleep(10)
        ensure_cloudfront_oai_bucket_policy(s3_bucket_name, oai_id)
    except ClientError as e:
        logger.error(f"Failed to update S3 bucket policy: {e}")
        logger.error(f"OAI ID: {oai_id}")
        raise

    # Create CloudFront distribution with both ALB and S3 origins (matching provided config format)
    logger.info("  Creating CloudFront distribution with ALB and S3 origins...")
    distribution_config = {
        "CallerReference": f"{project_name}-{int(time.time())}",
        "Comment": _cloudfront_distribution_comment(),
        "DefaultCacheBehavior": {
            "TargetOriginId": f"alb-{project_name}",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 7,
                "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
                "CachedMethods": {
                    "Quantity": 2,
                    "Items": ["GET", "HEAD"]
                }
            },
            "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
            "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3",
            "ResponseHeadersPolicyId": cloudfront_response_headers_policy_id(),
            "Compress": True
        },
        "CacheBehaviors": {
            "Quantity": 3,
            "Items": [
                _cloudfront_s3_cache_behavior(
                    "/images/*", f"s3-{project_name}", key_group_id
                ),
                _cloudfront_s3_cache_behavior(
                    "/docs/*", f"s3-{project_name}", key_group_id
                ),
                _cloudfront_s3_cache_behavior(
                    "/artifacts/*", f"s3-{project_name}", key_group_id
                ),
            ]
        },
        "Origins": {
            "Quantity": 2,
            "Items": [
                {
                    "Id": f"alb-{project_name}",
                    "DomainName": alb_info["dns"],
                    "CustomOriginConfig": _cloudfront_alb_custom_origin_config(),
                    "CustomHeaders": _cloudfront_alb_origin_custom_headers(
                        origin_header_value
                    ),
                    "OriginPath": ""
                },
                {
                    "Id": f"s3-{project_name}",
                    "DomainName": f"{s3_bucket_name}.s3.{region}.amazonaws.com",
                    "S3OriginConfig": {
                        "OriginAccessIdentity": f"origin-access-identity/cloudfront/{oai_id}"
                    },
                    "CustomHeaders": {
                        "Quantity": 0,
                        "Items": []
                    },
                    "OriginPath": ""
                }
            ]
        },
        "Enabled": True,
        "PriceClass": "PriceClass_200"
    }
    
    # Log distribution config to verify it matches the expected format
    logger.info(f"Creating CloudFront distribution with config:")
    logger.info(f"  Origins: {[origin['Id'] for origin in distribution_config['Origins']['Items']]}")
    logger.info(f"  DefaultCacheBehavior TargetOriginId: {distribution_config['DefaultCacheBehavior']['TargetOriginId']}")
    logger.info(f"  CacheBehaviors: {len(distribution_config['CacheBehaviors']['Items'])} behaviors")
    
    try:
        response = cloudfront_client.create_distribution(DistributionConfig=distribution_config)
        distribution_id = response["Distribution"]["Id"]
        distribution_domain = response["Distribution"]["DomainName"]
        
        logger.info(f"✓ CloudFront distribution created (ALB + S3): {distribution_domain}")
        logger.info(f"  Distribution ID: {distribution_id}")
        logger.info(f"  Default origin: ALB {alb_info['dns']}")
        logger.info(f"  /images/*, /docs/*, /artifacts/* origins: S3 bucket {s3_bucket_name}")
        logger.warning("  Note: CloudFront distribution may take 15-20 minutes to deploy")
        
    except ClientError as e:
        logger.error(f"Error creating CloudFront distribution: {e}")
        raise
    
    return {
        "id": distribution_id,
        "domain": distribution_domain,
        "key_pair_id": signing.get("public_key_id") or "",
        "key_group_id": key_group_id,
        "private_key_pem": signing.get("private_key_pem") or "",
    }


def _load_runtime_agent_config(runtime_type: str = "strands") -> Dict[str, str]:
    """Load runtime_agent/<type>/config.json written by the Agent Runtime installer."""
    config_path = os.path.join(_project_root(), "runtime_agent", runtime_type, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read runtime agent config ({config_path}): {e}")
        return {}


def _merge_runtime_agent_settings(app_config: Dict[str, str]) -> Dict[str, str]:
    """Attach AgentCore runtime ARNs from runtime_agent config into application config."""
    runtime_config = _load_runtime_agent_config("strands")
    agent_runtime_arn = runtime_config.get("agent_runtime_arn")
    if agent_runtime_arn:
        app_config["agent_runtime_arn"] = agent_runtime_arn
    agent_runtime_role = runtime_config.get("agent_runtime_role")
    if agent_runtime_role:
        app_config["agent_runtime_role"] = agent_runtime_role
    return app_config


def build_app_environment(
    knowledge_base_role_arn: str,
    opensearch_info: Dict[str, str],
    s3_bucket_name: str,
    cloudfront_domain: str,
    knowledge_base_id: str,
    data_source_id: Optional[str] = None,
    agentcore_websearch_gateway_info: Optional[Dict[str, str]] = None,
    cognito_info: Optional[Dict[str, str]] = None,
    agentcore_memory_role_arn: str = "",
    memory_id: str = "",
) -> Dict[str, str]:
    """Build application config used by the container at runtime."""
    app_config = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
        "knowledge_base_id": knowledge_base_id,
        "data_source_id": data_source_id if data_source_id else "",
        "knowledge_base_role": knowledge_base_role_arn,
        "collectionArn": opensearch_info["arn"],
        "opensearch_url": opensearch_info["endpoint"],
        "vector_bucket_name": "",
        "vector_bucket_arn": "",
        "vector_index_name": vector_index_name,
        "vector_index_arn": "",
        "s3_bucket": s3_bucket_name,
        "s3_arn": f"arn:aws:s3:::{s3_bucket_name}",
        "sharing_url": f"https://{cloudfront_domain}",
    }
    if cognito_info:
        app_config.update({
            "cognito_user_pool_id": cognito_info.get("cognito_user_pool_id", ""),
            "cognito_user_pool_name": cognito_info.get("cognito_user_pool_name", ""),
            "cognito_client_id": cognito_info.get("cognito_client_id", ""),
            "cognito_client_name": cognito_info.get("cognito_client_name", ""),
            "cognito_admin_username": cognito_info.get("cognito_admin_username", ""),
            "cognito_region": cognito_info.get("cognito_region", region),
        })
    if agentcore_memory_role_arn:
        app_config["agentcore_memory_role"] = agentcore_memory_role_arn
    if memory_id:
        app_config["memory_id"] = memory_id
    _apply_websearch_gateway_config(app_config, agentcore_websearch_gateway_info)
    return _merge_runtime_agent_settings(app_config)


def _application_config_path() -> str:
    project_root = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(project_root, "application", "config.json")


def _project_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _skill_name_from_skill_md(skill_md_path: str, fallback: str) -> str:
    """Read SKILL.md YAML frontmatter `name`, falling back to the directory name."""
    try:
        with open(skill_md_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return fallback

    if not raw.startswith("---"):
        return fallback

    parts = raw.split("---", 2)
    if len(parts) < 3:
        return fallback

    for line in parts[1].splitlines():
        stripped = line.strip()
        if stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            return value or fallback
    return fallback


def update_skills_list_from_directory() -> List[str]:
    """Rewrite application/skills.list from runtime_agent/strands/skills/*/SKILL.md names.

    Runtime no longer ships a strands/skills.list; per-user lists live under
    {SESSION_STORAGE_DIR}/{user_id}/skills.list. application/skills.list is the
    builtin seed used when those per-user lists are created.
    """
    skills_dir = os.path.join(_project_root(), "runtime_agent", "strands", "skills")
    list_path = os.path.join(_project_root(), "application", "skills.list")

    if not os.path.isdir(skills_dir):
        raise FileNotFoundError(f"Missing skills directory: {skills_dir}")

    skill_names: List[str] = []
    for entry in os.listdir(skills_dir):
        skill_md = os.path.join(skills_dir, entry, "SKILL.md")
        if os.path.isfile(skill_md):
            skill_names.append(_skill_name_from_skill_md(skill_md, entry))

    ordered_names = sorted(set(skill_names))

    os.makedirs(os.path.dirname(list_path), exist_ok=True)
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ordered_names))
        if ordered_names:
            f.write("\n")

    logger.info(
        f"  ✓ Updated application/skills.list from {skills_dir} "
        f"({len(ordered_names)} skill(s)): {', '.join(ordered_names)}"
    )
    return ordered_names


def sync_application_capability_lists() -> None:
    """Refresh application capability lists before image build.

    - skills.list: rebuild from runtime_agent/strands/skills/*/SKILL.md
    - mcp.list: copy from runtime_agent/strands/mcp.list
    """
    update_skills_list_from_directory()

    src = os.path.join(_project_root(), "runtime_agent", "strands", "mcp.list")
    dst = os.path.join(_project_root(), "application", "mcp.list")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"Missing capability list: {src}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    logger.info("  ✓ Overwrote application/mcp.list from runtime_agent/strands/mcp.list")


def write_application_config(config_data: Dict, *, merge_existing: bool = True) -> bool:
    """Write application/config.json for local development and ECS runtime."""
    config_path = _application_config_path()
    data = dict(config_data)

    if merge_existing:
        try:
            with open(config_path, "r") as f:
                data = {**json.load(f), **data}
        except FileNotFoundError:
            logger.info(f"Creating new {config_path}")
        except Exception as e:
            logger.warning(f"Could not read existing {config_path}: {e}")

    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Could not write {config_path}: {e}")
        return False


def build_config_from_deployment_state(
    knowledge_base_id: Optional[str] = None,
    data_source_id: Optional[str] = None,
    knowledge_base_role_arn: Optional[str] = None,
    opensearch_info: Optional[Dict[str, str]] = None,
    s3_bucket_name: Optional[str] = None,
    cloudfront_info: Optional[Dict[str, str]] = None,
    s3_files_info: Optional[Dict[str, object]] = None,
    s3_files_app_data_info: Optional[Dict[str, object]] = None,
    cognito_info: Optional[Dict[str, str]] = None,
    agentcore_memory_role_arn: Optional[str] = None,
    memory_id: Optional[str] = None,
) -> Dict[str, str]:
    """Build config.json payload from whatever deployment resources are available."""
    config_data: Dict[str, str] = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
        "collectionArn": "",
        "opensearch_url": "",
        "vector_bucket_name": "",
        "vector_bucket_arn": "",
        "vector_index_name": "",
        "vector_index_arn": "",
    }
    if knowledge_base_id:
        config_data["knowledge_base_id"] = knowledge_base_id
    if data_source_id:
        config_data["data_source_id"] = data_source_id
    if knowledge_base_role_arn:
        config_data["knowledge_base_role"] = knowledge_base_role_arn
    if opensearch_info:
        config_data["collectionArn"] = opensearch_info.get("arn", "")
        config_data["opensearch_url"] = opensearch_info.get("endpoint", "")
        config_data["vector_index_name"] = vector_index_name
    if s3_bucket_name:
        config_data["s3_bucket"] = s3_bucket_name
        config_data["s3_arn"] = f"arn:aws:s3:::{s3_bucket_name}"
    if cloudfront_info:
        config_data["sharing_url"] = f"https://{cloudfront_info.get('domain', '')}"
    if cognito_info:
        config_data.update({
            "cognito_user_pool_id": cognito_info.get("cognito_user_pool_id", ""),
            "cognito_user_pool_name": cognito_info.get("cognito_user_pool_name", ""),
            "cognito_client_id": cognito_info.get("cognito_client_id", ""),
            "cognito_client_name": cognito_info.get("cognito_client_name", ""),
            "cognito_admin_username": cognito_info.get("cognito_admin_username", ""),
            "cognito_region": cognito_info.get("cognito_region", region),
        })
    if agentcore_memory_role_arn:
        config_data["agentcore_memory_role"] = agentcore_memory_role_arn
    if memory_id:
        config_data["memory_id"] = memory_id
    config_data = apply_s3_files_config(
        config_data, s3_files_info, s3_files_app_data_info
    )
    return _merge_runtime_agent_settings(config_data)


def create_ecr_repository() -> str:
    """Create ECR repository and return repository URI."""
    logger.info("[8/10] Creating ECR repository")
    repository_name = f"ecr-for-{project_name}"

    try:
        response = ecr_client.create_repository(
            repositoryName=repository_name,
            imageScanningConfiguration={"scanOnPush": True},
            imageTagMutability="MUTABLE",
        )
        repository_uri = response["repository"]["repositoryUri"]
        logger.info(f"  ✓ Created ECR repository: {repository_uri}")
        return repository_uri
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryAlreadyExistsException":
            response = ecr_client.describe_repositories(repositoryNames=[repository_name])
            repository_uri = response["repositories"][0]["repositoryUri"]
            logger.warning(f"  ECR repository already exists: {repository_uri}")
            return repository_uri
        raise


def _run_command(command: List[str], cwd: Optional[str] = None) -> None:
    """Run a shell command and raise on failure."""
    logger.debug(f"Running command: {' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        logger.debug(result.stdout.strip())
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else "Unknown error"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{stderr}")


def _run_command_streaming(command: List[str], cwd: Optional[str] = None) -> None:
    """Run a shell command and stream combined stdout/stderr to the logger."""
    logger.info(f"  $ {' '.join(command)}")
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "BUILDKIT_PROGRESS": "plain"}
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    if process.stdout:
        for line in iter(process.stdout.readline, ""):
            stripped = line.rstrip("\r\n")
            if stripped:
                logger.info(f"  | {stripped}")
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(f"Command failed ({returncode}): {' '.join(command)}")


def generate_image_build_tag() -> str:
    """Generate a unique Docker image tag for this deployment."""
    return datetime.now().strftime("%Y%m%d%H%M%S")


def resolve_ecr_image_uri(repository_uri: str, image_tag: Optional[str] = None) -> str:
    """Resolve ECR image URI from explicit tag, config, or newest pushed image."""
    if image_tag:
        return f"{repository_uri}:{image_tag}"

    config_path = _application_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        saved_tag = config_data.get("latest_image_tag") or config_data.get("build_number")
        if saved_tag:
            logger.info(f"  Using saved build tag from config: {saved_tag}")
            return f"{repository_uri}:{saved_tag}"
    except (OSError, json.JSONDecodeError):
        pass

    repository_name = repository_uri.rsplit("/", 1)[-1]
    try:
        response = ecr_client.describe_images(
            repositoryName=repository_name,
            filter={"tagStatus": "TAGGED"},
        )
        images = response.get("imageDetails", [])
        if images:
            latest_image = sorted(images, key=lambda x: x["imagePushedAt"], reverse=True)[0]
            tags = latest_image.get("imageTags") or []
            if tags:
                resolved_tag = tags[0]
                logger.info(f"  Using latest ECR image tag: {resolved_tag}")
                return f"{repository_uri}:{resolved_tag}"
    except ClientError as e:
        logger.warning(f"  Could not resolve latest ECR image tag: {e}")

    logger.warning("  Falling back to image tag: latest")
    return f"{repository_uri}:latest"


DOCKER_MIN_FREE_MB = 2048
DOCKER_REQUIRED_FREE_MB = 1024


def _docker_daemon_reachable(timeout: int = 20) -> Tuple[bool, str]:
    """Return (ok, detail) for whether the local Docker daemon accepts commands."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            return True, ""
        return False, (result.stderr or result.stdout).strip()
    except subprocess.TimeoutExpired:
        return False, f"docker info timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


def _try_start_docker_daemon() -> bool:
    """Best-effort start of the Docker service on Linux hosts."""
    commands = [
        ["systemctl", "start", "docker"],
        ["sudo", "systemctl", "start", "docker"],
        ["service", "docker", "start"],
        ["sudo", "service", "docker", "start"],
    ]
    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode == 0:
                logger.info(f"  Started Docker via: {' '.join(cmd)}")
                return True
        except Exception:
            continue
    return False


def ensure_docker_daemon_running() -> None:
    """Require a reachable Docker daemon before image build/push."""
    ok, detail = _docker_daemon_reachable()
    if ok:
        logger.info("  ✓ Docker daemon is running")
        return

    logger.warning(f"  Docker daemon not reachable: {detail}")
    logger.info("  Attempting to start Docker service...")
    if _try_start_docker_daemon():
        time.sleep(2)
        ok, detail = _docker_daemon_reachable()
        if ok:
            logger.info("  ✓ Docker daemon is running")
            return

    raise RuntimeError(
        "Docker daemon is not available.\n"
        f"  Detail: {detail or 'unknown'}\n"
        "  On Amazon Linux / EC2, run:\n"
        "    sudo dnf install -y docker   # or: sudo yum install -y docker\n"
        "    sudo systemctl start docker\n"
        "    sudo systemctl enable docker\n"
        "    sudo usermod -aG docker $USER\n"
        "    newgrp docker\n"
        "    docker info\n"
        "  Then rerun installer.py (or use --skip-docker-build if the image is already in ECR)."
    )


def _host_is_arm64() -> bool:
    return os.uname().machine.lower() in ("aarch64", "arm64")


def _docker_build_platform() -> str:
    """Return Docker platform for ECS/AgentCore (linux/arm64, native build only)."""
    return "linux/arm64"


def _ecs_runtime_platform() -> Dict[str, str]:
    """Return ECS Fargate runtimePlatform (ARM64, aligned with AgentCore runtime)."""
    return {
        "cpuArchitecture": "ARM64",
        "operatingSystemFamily": "LINUX",
    }


def _require_arm64_build_host(context: str) -> None:
    """Ensure Docker images are built natively on ARM64 (same as AgentCore runtime)."""
    if _host_is_arm64():
        return
    raise RuntimeError(
        f"{context} requires linux/arm64 images.\n"
        f"  Current host architecture: {os.uname().machine}\n"
        "  Build on an ARM64 EC2 instance (e.g. t4g, m7g) and retry."
    )


NATIVE_BUILDX_BUILDER = "ecs-native-builder"


def _normalize_buildx_builder_name(name: str) -> str:
    return name.strip().rstrip("*")


def _list_docker_driver_builders() -> list[str]:
    """Return buildx builder names that use the docker driver."""
    builders: list[str] = []

    fmt = subprocess.run(
        ["docker", "buildx", "ls", "--format", "{{.Name}} {{.Driver}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if fmt.returncode == 0:
        for line in fmt.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[1] == "docker":
                name = _normalize_buildx_builder_name(parts[0])
                if name and name not in builders:
                    builders.append(name)

    if not builders:
        result = subprocess.run(
            ["docker", "buildx", "ls"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                # Skip header and node rows (" \_ nodename ...")
                if not line.strip() or line.startswith("NAME") or line[:1].isspace():
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                name = _normalize_buildx_builder_name(parts[0])
                driver = parts[1]
                if driver == "docker" and name not in builders:
                    builders.append(name)

    for fallback_name in ("default", "desktop-linux"):
        if fallback_name in builders:
            continue
        inspect = subprocess.run(
            ["docker", "buildx", "inspect", fallback_name],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if inspect.returncode == 0:
            builders.append(fallback_name)

    return builders


def _current_docker_context() -> Optional[str]:
    result = subprocess.run(
        ["docker", "context", "show"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def _use_buildx_builder(name: str) -> None:
    use = subprocess.run(
        ["docker", "buildx", "use", name],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if use.returncode != 0:
        err = (use.stderr or use.stdout).strip()
        raise RuntimeError(f"Failed to select buildx builder '{name}': {err}")


def _select_existing_docker_builder() -> Optional[str]:
    """Pick a usable docker-driver builder for the current Docker context."""
    existing = _list_docker_driver_builders()
    if not existing:
        return None

    context = _current_docker_context()
    # Prefer the builder matching the active context (e.g. desktop-linux),
    # then other well-known local builders. "default" often requires switching
    # Docker context and fails on Docker Desktop.
    candidates: list[str] = []
    for name in (context, "desktop-linux", "default", *existing):
        if name and name in existing and name not in candidates:
            candidates.append(name)

    last_error = None
    for name in candidates:
        try:
            _use_buildx_builder(name)
            return name
        except RuntimeError as exc:
            last_error = exc
            continue

    if last_error:
        raise last_error
    return None


def _ensure_native_buildx_builder() -> bool:
    """Ensure a usable buildx builder is selected for ECR push.

    Returns True when buildx is ready, False when callers should use classic
    ``docker build`` + ``docker push`` (native ARM64 EC2 hosts).
    """
    selected: Optional[str] = None

    inspect = subprocess.run(
        ["docker", "buildx", "inspect", NATIVE_BUILDX_BUILDER],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if inspect.returncode == 0:
        selected = NATIVE_BUILDX_BUILDER
    else:
        selected = _select_existing_docker_builder()
        if not selected:
            create = subprocess.run(
                [
                    "docker", "buildx", "create",
                    "--name", NATIVE_BUILDX_BUILDER,
                    "--driver", "docker",
                    "--use",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if create.returncode == 0:
                selected = NATIVE_BUILDX_BUILDER
            else:
                err = (create.stderr or create.stdout).strip()
                selected = _select_existing_docker_builder()
                if selected:
                    logger.info(
                        f"  Reusing existing docker-driver buildx builder: {selected} "
                        f"(cannot create additional docker-driver instances)"
                    )
                elif "additional instances of driver" in err:
                    logger.warning(
                        "  Could not create or select a buildx docker-driver builder; "
                        "falling back to classic docker build + push."
                    )
                    return False
                else:
                    logger.warning(
                        f"  buildx builder setup failed ({err}); "
                        "falling back to classic docker build + push."
                    )
                    return False

    if selected:
        _use_buildx_builder(selected)

    bootstrap = subprocess.run(
        ["docker", "buildx", "inspect", "--bootstrap"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if bootstrap.returncode != 0:
        err = (bootstrap.stderr or bootstrap.stdout).strip()
        logger.warning(
            f"  buildx bootstrap failed ({err}); "
            "falling back to classic docker build + push."
        )
        return False

    return True


def _build_and_push_with_buildx(
    image_uri: str,
    docker_platform: str,
    project_root: str,
) -> None:
    _run_command_streaming(
        [
            "docker", "buildx", "build",
            "--platform", docker_platform,
            "--provenance=false",
            "--sbom=false",
            "-t", image_uri,
            "--push",
            ".",
        ],
        cwd=project_root,
    )


def _build_and_push_with_classic_docker(
    image_uri: str,
    docker_platform: str,
    project_root: str,
) -> None:
    """Build and push without buildx (reliable on native ARM64 EC2)."""
    _run_command_streaming(
        [
            "docker", "build",
            "--platform", docker_platform,
            "-t", image_uri,
            ".",
        ],
        cwd=project_root,
    )
    _run_command_streaming(["docker", "push", image_uri], cwd=project_root)



def _docker_data_root() -> str:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.DockerRootDir}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "/var/lib/docker"


def _filesystem_free_mb(path: str) -> int:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1


def _cleanup_docker_resources() -> None:
    logger.info("  Cleaning up unused Docker data to reclaim disk space...")
    for cmd, label in [
        (["docker", "builder", "prune", "-af"], "BuildKit cache"),
        (["docker", "image", "prune", "-af"], "Unused images"),
        (["docker", "container", "prune", "-f"], "Stopped containers"),
        (["docker", "volume", "prune", "-f"], "Unused volumes"),
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
            if result.returncode == 0:
                output = (result.stdout or result.stderr).strip().splitlines()
                detail = output[-1] if output else "done"
                logger.info(f"  ✓ Pruned {label}: {detail}")
            else:
                err = (result.stderr or result.stdout).strip()
                logger.warning(f"  Failed to prune {label}: {err}")
        except Exception as e:
            logger.warning(f"  Failed to prune {label}: {e}")


def _ensure_docker_disk_space(min_free_mb: int = DOCKER_MIN_FREE_MB) -> None:
    docker_root = _docker_data_root()
    root_free = _filesystem_free_mb("/")
    docker_free = _filesystem_free_mb(docker_root)
    free_mb = min(root_free, docker_free) if root_free >= 0 and docker_free >= 0 else max(root_free, docker_free)
    logger.info(f"  Disk space: root={root_free} MB, docker={docker_free} MB ({docker_root})")

    if free_mb >= min_free_mb:
        logger.info(f"  ✓ Sufficient disk space ({free_mb} MB >= {min_free_mb} MB)")
        return

    logger.warning(
        f"  Low disk space ({free_mb} MB free, need ~{min_free_mb} MB). "
        "Attempting Docker cleanup..."
    )
    _cleanup_docker_resources()

    root_free = _filesystem_free_mb("/")
    docker_free = _filesystem_free_mb(docker_root)
    free_mb = min(root_free, docker_free) if root_free >= 0 and docker_free >= 0 else max(root_free, docker_free)
    logger.info(f"  Disk space after cleanup: root={root_free} MB, docker={docker_free} MB")

    if free_mb < DOCKER_REQUIRED_FREE_MB:
        raise RuntimeError(
            "Not enough disk space for Docker build. "
            "Run 'docker system prune -af', free space under /var/lib/docker, "
            "or use --skip-docker-build with an image built elsewhere."
        )


def _promote_ecr_image_tag(
    repository_uri: str, source_tag: str, target_tag: str = "latest"
) -> None:
    """Point an ECR tag at an already-pushed manifest (avoids docker push :latest index conflicts)."""
    repository_name = repository_uri.rsplit("/", 1)[-1]
    response = ecr_client.batch_get_image(
        repositoryName=repository_name,
        imageIds=[{"imageTag": source_tag}],
        acceptedMediaTypes=[
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
        ],
    )
    images = response.get("images") or []
    if not images:
        raise RuntimeError(
            f"ECR image not found for tag {source_tag} in repository {repository_name}"
        )
    image = images[0]
    put_params: Dict[str, str] = {
        "repositoryName": repository_name,
        "imageTag": target_tag,
        "imageManifest": image["imageManifest"],
    }
    media_type = image.get("imageManifestMediaType")
    if media_type:
        put_params["imageManifestMediaType"] = media_type
    ecr_client.put_image(**put_params)
    logger.info(f"  ✓ Promoted ECR tag {source_tag} -> {target_tag}")


def build_and_push_docker_image(
    repository_uri: str, image_tag: Optional[str] = None
) -> Tuple[str, str]:
    """Build Docker image from Dockerfile and push to ECR."""
    logger.info("[8/10] Building and pushing Docker image to ECR")

    # Rebuild application/skills.list from skills/; copy mcp.list before image build.
    sync_application_capability_lists()

    if shutil.which("docker") is None:
        raise RuntimeError("Docker CLI is required to build and push the container image")

    ensure_docker_daemon_running()
    _require_arm64_build_host("ECS container image build")

    if not image_tag:
        image_tag = generate_image_build_tag()

    registry = repository_uri.split("/")[0]
    image_uri = f"{repository_uri}:{image_tag}"
    project_root = os.path.dirname(os.path.abspath(__file__))

    logger.info(f"  Build number (image tag): {image_tag}")

    login_cmd = [
        "aws", "ecr", "get-login-password",
        "--region", region,
    ]
    login_result = subprocess.run(login_cmd, capture_output=True, text=True, check=False)
    if login_result.returncode != 0:
        raise RuntimeError(f"Failed to get ECR login password: {login_result.stderr.strip()}")

    docker_login = subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=login_result.stdout,
        capture_output=True,
        text=True,
        check=False,
    )
    if docker_login.returncode != 0:
        raise RuntimeError(f"Docker login to ECR failed: {docker_login.stderr.strip()}")

    _ensure_docker_disk_space()

    docker_platform = _docker_build_platform()
    logger.info(f"  Host architecture: {os.uname().machine}")
    logger.info(f"  Docker platform: {docker_platform} (native ARM64 build, no QEMU)")
    use_buildx = _ensure_native_buildx_builder()
    logger.info(f"  Starting Docker build+push: {image_uri}")
    logger.info("  Build output streams below (this may take several minutes)...")
    if use_buildx:
        # Push directly from buildx to avoid Docker Desktop containerd digest mismatch
        # between `docker build` and `docker push` on large multi-stage images.
        _build_and_push_with_buildx(image_uri, docker_platform, project_root)
    else:
        logger.info("  Using classic docker build + push (no buildx)")
        _build_and_push_with_classic_docker(image_uri, docker_platform, project_root)
    logger.info("  ✓ Docker build and push completed")
    logger.info(f"  Promoting {image_tag} to latest in ECR (avoid stale manifest list push)")
    _promote_ecr_image_tag(repository_uri, image_tag, "latest")
    logger.info(f"  ✓ Pushed image: {image_uri}")

    _cleanup_docker_resources()
    return image_uri, image_tag



def create_ecs_log_group() -> str:
    """Create CloudWatch log group for ECS tasks."""
    log_group_name = f"/ecs/app-for-{project_name}"
    try:
        logs_client.create_log_group(logGroupName=log_group_name)
        logger.info(f"  ✓ Created log group: {log_group_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        logger.warning(f"  Log group already exists: {log_group_name}")
    return log_group_name


ECS_SERVICE_LINKED_ROLE_NAME = "AWSServiceRoleForECS"


def ensure_ecs_service_linked_role() -> None:
    """Ensure the ECS service-linked role exists.

    ECS requires AWSServiceRoleForECS when creating Fargate services with
    awvpc networking and Application Load Balancer target groups.
    """
    logger.debug(f"Checking ECS service-linked role: {ECS_SERVICE_LINKED_ROLE_NAME}")
    try:
        iam_client.get_role(RoleName=ECS_SERVICE_LINKED_ROLE_NAME)
        logger.info(f"  ✓ ECS service-linked role already exists: {ECS_SERVICE_LINKED_ROLE_NAME}")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    logger.info("  Creating ECS service-linked role...")
    try:
        iam_client.create_service_linked_role(
            AWSServiceName="ecs.amazonaws.com",
            Description="Service-linked role for Amazon ECS.",
        )
        logger.info(f"  ✓ Created ECS service-linked role: {ECS_SERVICE_LINKED_ROLE_NAME}")
        # IAM propagation can take a few seconds before ECS can assume the role.
        time.sleep(10)
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_message = e.response["Error"].get("Message", str(e))
        if error_code == "InvalidInputException" and "has been taken" in error_message:
            logger.warning(f"  ECS service-linked role already exists: {ECS_SERVICE_LINKED_ROLE_NAME}")
            return
        if error_code in {"AccessDenied", "AccessDeniedException"}:
            raise PermissionError(
                "Missing iam:CreateServiceLinkedRole permission. Create the ECS "
                "service-linked role manually, then rerun the installer:\n"
                "  aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com"
            ) from e
        raise


def create_ecs_cluster() -> str:
    """Create ECS cluster."""
    cluster_name = f"cluster-for-{project_name}"
    try:
        response = ecs_client.create_cluster(
            clusterName=cluster_name,
            tags=[{"key": "Name", "value": cluster_name}],
        )
        cluster_arn = response["cluster"]["clusterArn"]
        logger.info(f"  ✓ Created ECS cluster: {cluster_name}")
        return cluster_arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        clusters = ecs_client.describe_clusters(clusters=[cluster_name])
        if clusters["clusters"]:
            return clusters["clusters"][0]["clusterArn"]
        raise


def create_alb_target_group_for_ecs(vpc_info: Dict[str, str]) -> str:
    """Create ALB target group for ECS Fargate (IP target type)."""
    target_port = 8501
    target_group_name = f"TG-for-{project_name}"
    tg_arn = None

    try:
        tgs = elbv2_client.describe_target_groups(Names=[target_group_name])
        if tgs["TargetGroups"]:
            tg = tgs["TargetGroups"][0]
            if tg.get("TargetType") != "ip":
                raise ValueError(
                    f"Existing target group {target_group_name} uses TargetType="
                    f"{tg.get('TargetType')}. Delete it or rename before ECS deployment."
                )
            tg_arn = tg["TargetGroupArn"]
            logger.warning(f"  Target group already exists: {tg_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "TargetGroupNotFound":
            raise

    if not tg_arn:
        tg_response = elbv2_client.create_target_group(
            Name=target_group_name,
            Protocol="HTTP",
            Port=target_port,
            VpcId=vpc_info["vpc_id"],
            TargetType="ip",
            HealthCheckProtocol="HTTP",
            HealthCheckPath="/api/health",
            HealthCheckIntervalSeconds=30,
            HealthCheckTimeoutSeconds=5,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
        )
        tg_arn = tg_response["TargetGroups"][0]["TargetGroupArn"]
        logger.info(f"  ✓ Created ECS target group: {tg_arn}")

    # Prefer application-cookie stickiness over lb_cookie.
    # Duration-based lb_cookie emits AWSALB / AWSALBCORS without configurable
    # Secure/HttpOnly flags (AWS platform limitation). Binding to our HMAC session
    # cookie (agent_user_id: HttpOnly + Secure on HTTPS) keeps same-target routing
    # for SQLite working-copy consistency without those ALB cookies.
    try:
        elbv2_client.modify_target_group_attributes(
            TargetGroupArn=tg_arn,
            Attributes=[
                {"Key": "stickiness.enabled", "Value": "true"},
                {"Key": "stickiness.type", "Value": "app_cookie"},
                {"Key": "stickiness.app_cookie.cookie_name", "Value": "agent_user_id"},
                {"Key": "stickiness.app_cookie.duration_seconds", "Value": "86400"},
            ],
        )
        logger.info(
            "  ✓ Enabled ALB target group stickiness "
            "(app_cookie=agent_user_id, 86400s)"
        )
    except ClientError as e:
        logger.warning(f"  Could not enable target group stickiness: {e}")

    return tg_arn


def create_alb_listener_with_target_group(
    alb_info: Dict[str, str],
    tg_arn: str,
    origin_header_value: str,
) -> str:
    """Create ALB listener: 403 by default, forward only with origin header."""
    return ensure_alb_listener_origin_protection(
        alb_info["arn"], tg_arn, origin_header_value
    )


def _ensure_private_subnets(vpc_info: Dict[str, str]) -> List[str]:
    """Return available private subnet IDs for ECS tasks."""
    private_subnets = vpc_info.get("private_subnets", [])
    if not private_subnets:
        logger.warning("  No private subnets in vpc_info, attempting to refresh from AWS...")
        vpc_id = vpc_info.get("vpc_id")
        if vpc_id:
            subnets_response = ec2_client.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            classified = classify_subnets(subnets_response["Subnets"], filter_available=True)
            private_subnets = classified["private_subnets"]
            if private_subnets:
                vpc_info["private_subnets"] = private_subnets

    if not private_subnets:
        raise ValueError(
            f"No private subnets found in VPC {vpc_info.get('vpc_id', 'unknown')}. "
            "ECS tasks require at least one private subnet."
        )

    available_subnets = []
    for subnet_id in private_subnets:
        try:
            response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            if response["Subnets"] and response["Subnets"][0]["State"] == "available":
                available_subnets.append(subnet_id)
        except Exception as e:
            logger.warning(f"  Could not verify subnet {subnet_id}: {e}")

    if not available_subnets:
        for subnet_id in private_subnets:
            if wait_for_subnet_available(subnet_id, max_wait_time=60):
                available_subnets.append(subnet_id)

    if not available_subnets:
        raise ValueError(
            f"No available private subnets found in VPC {vpc_info.get('vpc_id', 'unknown')}."
        )

    vpc_info["private_subnets"] = available_subnets
    return available_subnets


def _ecs_primary_deployment_ready(service: Dict[str, object]) -> bool:
    """Return True when the PRIMARY deployment is serving the desired task count."""
    if service.get("status") != "ACTIVE":
        return False

    deployments = service.get("deployments") or []
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if not primary:
        return False

    desired = primary.get("desiredCount", 0)
    running = primary.get("runningCount", 0)
    pending = primary.get("pendingCount", 0)
    # Only evaluate the PRIMARY deployment. During rolling updates the service-level
    # runningCount can exceed desiredCount while the previous deployment drains
    # (minimumHealthyPercent=100%, ALB deregistration delay up to 300s).
    return (
        desired > 0
        and running == desired
        and pending == 0
        and service.get("pendingCount", 0) == 0
    )


def _describe_ecs_service(cluster_name: str, service_name: str) -> Dict[str, object]:
    response = ecs_client.describe_services(cluster=cluster_name, services=[service_name])
    services = response.get("services") or []
    if not services:
        raise RuntimeError(f"ECS service not found: {service_name}")
    return services[0]


def _ecs_az_rebalancing_enabled(service: Dict[str, object]) -> bool:
    return (service.get("availabilityZoneRebalancing") or "").upper() == "ENABLED"


def _ecs_single_task_deployment_config(
    service: Optional[Dict[str, object]] = None,
) -> Optional[Dict[str, int]]:
    """Return 0/100 deployment config when compatible, else None (keep service default)."""
    if service and _ecs_az_rebalancing_enabled(service):
        return None
    return {"minimumHealthyPercent": 0, "maximumPercent": 100}


def _update_ecs_service_task_definition(
    cluster_name: str,
    service_name: str,
    task_definition_arn: str,
    existing_service: Dict[str, object],
) -> None:
    deployment_config = _ecs_single_task_deployment_config(existing_service)
    update_kwargs: Dict[str, object] = {
        "cluster": cluster_name,
        "service": service_name,
        "taskDefinition": task_definition_arn,
        "desiredCount": 1,
    }
    if deployment_config:
        update_kwargs["deploymentConfiguration"] = deployment_config
    try:
        ecs_client.update_service(**update_kwargs)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        error_message = exc.response.get("Error", {}).get("Message", "")
        if error_code == "InvalidParameterException" and (
            "Availability Zone Rebalancing" in error_message
            or "maximumPercent" in error_message
        ):
            logger.warning(
                "  ECS AZ Rebalancing enabled; retrying update with service-default "
                "deployment configuration"
            )
            ecs_client.update_service(
                cluster=cluster_name,
                service=service_name,
                taskDefinition=task_definition_arn,
                desiredCount=1,
                forceNewDeployment=True,
            )
        else:
            raise
    logger.info("  ✓ Updated ECS service with new task definition")


def _wait_for_ecs_service_ready(
    cluster_name: str,
    service_name: str,
    *,
    target_group_arn: Optional[str] = None,
    expected_task_definition_arn: Optional[str] = None,
    timeout_seconds: int = 600,
    poll_interval_seconds: int = 15,
) -> None:
    """Wait until the PRIMARY ECS deployment is ready.

    The boto3 ``services_stable`` waiter also blocks on DRAINING deployments while
    ALB target deregistration (default 300s) completes. The service is already
    usable once the PRIMARY deployment reaches the desired task count.
    """
    deadline = time.time() + timeout_seconds
    last_log = 0.0

    while time.time() < deadline:
        service = _describe_ecs_service(cluster_name, service_name)
        deployments = service.get("deployments") or []
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
        draining = [d for d in deployments if d.get("status") == "DRAINING"]
        desired = (primary or {}).get("desiredCount", 0)
        running = (primary or {}).get("runningCount", 0)
        pending = (primary or {}).get("pendingCount", 0)

        healthy_targets = None
        if target_group_arn:
            try:
                health = elbv2_client.describe_target_health(
                    TargetGroupArn=target_group_arn
                )
                healthy_targets = sum(
                    1
                    for target in health.get("TargetHealthDescriptions", [])
                    if target.get("TargetHealth", {}).get("State") == "healthy"
                )
            except ClientError as exc:
                logger.debug(f"  Target health check skipped: {exc}")

        if _ecs_primary_deployment_ready(service):
            if expected_task_definition_arn:
                primary_task_def = (primary or {}).get("taskDefinition", "")
                if primary_task_def != expected_task_definition_arn:
                    now = time.time()
                    if now - last_log > 30:
                        logger.info(
                            "  ... service status=%s running=%s/%s pending=%s",
                            service.get("status"),
                            running,
                            desired,
                            pending,
                        )
                        last_log = now
                    time.sleep(poll_interval_seconds)
                    continue
            if not target_group_arn or (healthy_targets or 0) > 0:
                if draining:
                    logger.info(
                        "✓ ECS PRIMARY deployment ready "
                        f"(running={running}/{desired}); "
                        f"{len(draining)} draining deployment(s) still cleaning up"
                    )
                else:
                    logger.info("✓ ECS service is stable")
                return

        now = time.time()
        if now - last_log > 30:
            logger.info(
                "  ... service status=%s running=%s/%s pending=%s",
                service.get("status"),
                running,
                desired,
                pending,
            )
            last_log = now
        time.sleep(poll_interval_seconds)

    service = _describe_ecs_service(cluster_name, service_name)
    deployments = service.get("deployments") or []
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if _ecs_primary_deployment_ready(service):
        if expected_task_definition_arn:
            primary_task_def = (primary or {}).get("taskDefinition", "")
            if primary_task_def != expected_task_definition_arn:
                raise TimeoutError(
                    f"Timed out after {timeout_seconds}s waiting for ECS service {service_name} "
                    f"to roll out task definition {expected_task_definition_arn.rsplit('/', 1)[-1]} "
                    f"(PRIMARY still on {primary_task_def.rsplit('/', 1)[-1]}). "
                    "Check ECS service events for update_service failures."
                )
        logger.warning(
            "  ECS wait timed out after %ss, but PRIMARY deployment is ready; continuing",
            timeout_seconds,
        )
        return

    raise TimeoutError(
        f"Timed out after {timeout_seconds}s waiting for ECS service {service_name} "
        f"(running={service.get('runningCount')}/{service.get('desiredCount')}, "
        f"pending={service.get('pendingCount')}, "
        f"primary={((primary or {}).get('runningCount'))}/{((primary or {}).get('desiredCount'))}, "
        f"failedTasks={(primary or {}).get('failedTasks')}). "
        "Check ECS task stopped reason / CloudWatch logs "
        f"(/ecs/app-for-{project_name}) for container startup failures."
    )


def deploy_ecs_service(
    vpc_info: Dict[str, str],
    alb_info: Dict[str, str],
    ecs_roles: Dict[str, str],
    image_uri: str,
    app_environment: Dict[str, str],
    log_group_name: str,
    s3_files_info: Optional[Dict[str, object]] = None,
    origin_header_value: str = "",
) -> Dict[str, str]:
    """Create ECS task definition and Fargate service behind the ALB."""
    logger.info("[9/10] Deploying ECS Fargate service")

    if not origin_header_value:
        origin_header_value = get_or_create_alb_origin_header(rotate=False)

    execution_role_name = f"role-ecs-execution-for-{project_name}-{region}"
    attach_ecs_execution_secrets_policy(execution_role_name)
    get_or_create_session_signing_key(rotate=False)
    get_or_create_schedule_agent_token(rotate=False)
    cf_signing = get_or_create_cloudfront_signing_material(rotate=False)

    ensure_ecs_service_linked_role()

    if not vpc_info.get("ecs_sg_id"):
        raise ValueError(
            f"No ECS security group found in VPC {vpc_info.get('vpc_id', 'unknown')}."
        )

    private_subnets = _ensure_private_subnets(vpc_info)
    cluster_arn = create_ecs_cluster()
    tg_arn = create_alb_target_group_for_ecs(vpc_info)
    listener_arn = create_alb_listener_with_target_group(
        alb_info, tg_arn, origin_header_value
    )

    task_family = f"task-for-{project_name}"
    service_name = f"service-for-{project_name}"
    container_name = "app"
    app_data_mount = "/mnt/app-data"

    environment = [
        {
            "name": "APP_CONFIG_JSON",
            "value": json.dumps(app_environment),
        },
        {
            "name": "CLOUDFRONT_KEY_PAIR_ID",
            "value": cf_signing.get("public_key_id") or "",
        },
    ]
    container_definition: Dict[str, object] = {
        "name": container_name,
        "image": image_uri,
        "essential": True,
        "portMappings": [{"containerPort": 8501, "protocol": "tcp"}],
        "environment": environment,
        "secrets": [
            {
                "name": "SESSION_SIGNING_KEY",
                "valueFrom": _ecs_secret_value_from(SESSION_SIGNING_KEY_SECRET_NAME),
            },
            {
                "name": "SCHEDULE_AGENT_TOKEN",
                "valueFrom": _ecs_secret_value_from(SCHEDULE_AGENT_TOKEN_SECRET_NAME),
            },
            {
                "name": "CLOUDFRONT_SIGNING_PRIVATE_KEY",
                "valueFrom": _ecs_secret_value_from(
                    CLOUDFRONT_SIGNING_KEY_SECRET_NAME,
                    json_key="private_key_pem",
                ),
            },
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group_name,
                "awslogs-region": region,
                "awslogs-stream-prefix": "ecs",
            },
        },
        "healthCheck": {
            "command": [
                "CMD-SHELL",
                "curl -f http://localhost:8501/api/health || exit 1",
            ],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 60,
        },
    }

    volumes: List[Dict[str, object]] = []
    file_system_id = (s3_files_info or {}).get("file_system_id")
    access_point_arn = (s3_files_info or {}).get("access_point_arn")
    if file_system_id and access_point_arn:
        file_system_arn = (
            s3_files_info.get("file_system_arn")
            or f"arn:aws:s3files:{region}:{account_id}:file-system/{file_system_id}"
        )
        volume_name = "app-data"
        volumes.append(
            {
                "name": volume_name,
                "s3filesVolumeConfiguration": {
                    "fileSystemArn": file_system_arn,
                    "rootDirectory": "/",
                    "accessPointArn": access_point_arn,
                },
            }
        )
        container_definition["mountPoints"] = [
            {
                "sourceVolume": volume_name,
                "containerPath": app_data_mount,
                "readOnly": False,
            }
        ]
        environment.extend(
            [
                {"name": "TASK_DB_MOUNT", "value": app_data_mount},
                {"name": "TASK_DB_PROJECT", "value": project_name},
                # graph/settings/tasks.db live on app-data; skills are loaded via S3 API.
                {"name": "SESSION_STORAGE_DIR", "value": app_data_mount},
            ]
        )
        logger.info(
            "  ECS will mount app-data S3 Files at %s (prefix=app-data/)",
            app_data_mount,
        )

    task_def_kwargs: Dict[str, object] = {
        "family": task_family,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "1024",
        "memory": "2048",
        "runtimePlatform": _ecs_runtime_platform(),
        "executionRoleArn": ecs_roles["execution_role_arn"],
        "taskRoleArn": ecs_roles["task_role_arn"],
        "containerDefinitions": [container_definition],
    }
    if volumes:
        task_def_kwargs["volumes"] = volumes

    task_def_response = ecs_client.register_task_definition(**task_def_kwargs)
    task_definition_arn = task_def_response["taskDefinition"]["taskDefinitionArn"]
    logger.info(f"  ✓ Registered task definition: {task_definition_arn}")

    cluster_name = cluster_arn.split("/")[-1]
    service_arn = None
    services = ecs_client.describe_services(cluster=cluster_name, services=[service_name])
    service_list = services.get("services") or []
    if service_list and service_list[0].get("status") != "INACTIVE":
        existing_service = service_list[0]
        service_arn = existing_service["serviceArn"]
        logger.warning(f"  ECS service already exists: {service_name}")
        _update_ecs_service_task_definition(
            cluster_name,
            service_name,
            task_definition_arn,
            existing_service,
        )

    if not service_arn:
        service_response = ecs_client.create_service(
            cluster=cluster_name,
            serviceName=service_name,
            taskDefinition=task_definition_arn,
            desiredCount=1,
            launchType="FARGATE",
            deploymentConfiguration={
                "minimumHealthyPercent": 0,
                "maximumPercent": 100,
            },
            availabilityZoneRebalancing="DISABLED",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": private_subnets,
                    "securityGroups": [vpc_info["ecs_sg_id"]],
                    "assignPublicIp": "DISABLED",
                }
            },
            loadBalancers=[
                {
                    "targetGroupArn": tg_arn,
                    "containerName": container_name,
                    "containerPort": 8501,
                }
            ],
            healthCheckGracePeriodSeconds=120,
            tags=[{"key": "Name", "value": service_name}],
        )
        service_arn = service_response["service"]["serviceArn"]
        logger.info(f"  ✓ Created ECS service: {service_name}")

    logger.info("  Waiting for ECS service to become stable...")
    _wait_for_ecs_service_ready(
        cluster_name,
        service_name,
        target_group_arn=tg_arn,
        expected_task_definition_arn=task_definition_arn,
    )

    logger.info(f"✓ ECS service deployed in private subnets: {', '.join(private_subnets)}")
    return {
        "cluster_arn": cluster_arn,
        "service_arn": service_arn,
        "service_name": service_name,
        "task_definition_arn": task_definition_arn,
        "target_group_arn": tg_arn,
        "listener_arn": listener_arn,
    }


def get_setup_script(environment: Dict[str, str], git_name: str) -> str:
    """Generate setup script for EC2 instance."""
    return f"""#!/bin/bash
exec > >(tee /var/log/user-data.log) 2>&1
set -x

# Update system
yum update -y

# Install packages
yum install -y git docker

# Start docker
systemctl start docker
systemctl enable docker
usermod -aG docker ssm-user

# Restart docker to ensure clean state
systemctl restart docker
sleep 10

# Create ssm-user home if not exists
mkdir -p /home/ssm-user
chown ssm-user:ssm-user /home/ssm-user

# Clone repository
cd /home/ssm-user
rm -rf {git_name}
git clone https://github.com/kyopark2014/{git_name}
chown -R ssm-user:ssm-user {git_name}

# Create config.json
mkdir -p /home/ssm-user/{git_name}/application
cat > /home/ssm-user/{git_name}/application/config.json << 'EOF'
{json.dumps(environment)}
EOF
chown -R ssm-user:ssm-user /home/ssm-user/{git_name}

# Build and run docker with volume mount for config.json
cd /home/ssm-user/{git_name}
docker build -f Dockerfile -t agent-ui .
docker run -d --restart=always -p 8501:8501 -v $(pwd)/application/config.json:/app/application/config.json --name app agent-ui

# Make update.sh executable for manual execution via SSM
chmod a+rx update.sh

# Restart SSM agent to ensure proper registration
echo "Restarting SSM agent..." >> /var/log/user-data.log
systemctl restart amazon-ssm-agent
systemctl enable amazon-ssm-agent
sleep 10
systemctl status amazon-ssm-agent >> /var/log/user-data.log

echo "Setup completed successfully" >> /var/log/user-data.log
"""


def run_setup_script_via_ssm(instance_id: str, environment: Dict[str, str], git_name: str = "mcp") -> Dict[str, str]:
    """Run setup script on existing EC2 instance using SSM Run Command."""
    logger.info(f"Running setup script on EC2 instance {instance_id} via SSM")
    
    # Wait for SSM agent to be ready
    logger.debug("Waiting for SSM agent to be ready...")
    max_attempts = 30
    for attempt in range(max_attempts):
        try:
            response = ssm_client.describe_instance_information(
                Filters=[
                    {
                        "Key": "InstanceIds",
                        "Values": [instance_id]
                    }
                ]
            )
            if response.get("InstanceInformationList"):
                logger.debug("SSM agent is ready")
                break
        except Exception as e:
            logger.debug(f"SSM agent not ready yet (attempt {attempt + 1}/{max_attempts}): {e}")
        
        if attempt < max_attempts - 1:
            time.sleep(10)
        else:
            raise Exception(f"SSM agent not ready after {max_attempts * 10} seconds")
    
    # Get setup script
    script = get_setup_script(environment, git_name)
    
    # Run command via SSM
    try:
        logger.debug("Sending command via SSM Run Command...")
        response = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={
                "commands": [script],
                "workingDirectory": ["/"]
            },
            TimeoutSeconds=3600,
            Comment=f"Setup script for {project_name}"
        )
        
        command_id = response["Command"]["CommandId"]
        logger.info(f"✓ Command sent via SSM: {command_id}")
        
        # Wait for command to complete
        logger.info("Waiting for command to complete (this may take several minutes)...")
        while True:
            time.sleep(10)
            result = ssm_client.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            status = result["Status"]
            
            if status in ["Success", "Failed", "Cancelled", "TimedOut"]:
                if status == "Success":
                    logger.info(f"✓ Setup script completed successfully")
                    logger.debug(f"Output: {result.get('StandardOutputContent', '')}")
                else:
                    error_output = result.get("StandardErrorContent", "")
                    logger.error(f"Setup script failed with status: {status}")
                    logger.error(f"Error output: {error_output}")
                    raise Exception(f"Setup script failed: {status}\n{error_output}")
                break
            
            logger.debug(f"Command status: {status} (waiting...)")
        
        return {
            "command_id": command_id,
            "status": status,
            "output": result.get("StandardOutputContent", ""),
            "error": result.get("StandardErrorContent", "")
        }
    
    except ClientError as e:
        logger.error(f"Failed to run setup script via SSM: {e}")
        raise


def create_ec2_instance(vpc_info: Dict[str, str], ec2_role_arn: str, 
                       knowledge_base_role_arn: str, opensearch_info: Dict[str, str],
                       s3_bucket_name: str, cloudfront_domain: str, knowledge_base_id: str,
                       data_source_id: str = None) -> str:
    """Create EC2 instance."""
    logger.info("[8/10] Creating EC2 instance")
    
    instance_name = f"app-for-{project_name}"
    
    # Check if EC2 instance already exists
    try:
        instances = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [instance_name]},
                {"Name": "instance-state-name", "Values": ["running", "pending", "stopping", "stopped"]}
            ]
        )
        for reservation in instances["Reservations"]:
            for instance in reservation["Instances"]:
                logger.warning(f"EC2 instance already exists: {instance['InstanceId']}")
                return instance["InstanceId"]
    except Exception as e:
        logger.debug(f"No existing EC2 instance found: {e}")
    
    # Get latest Amazon Linux 2023 ECS optimized AMI
    logger.debug("Finding latest Amazon Linux 2023 ECS optimized AMI")
    amis = ec2_client.describe_images(
        Owners=["amazon"],
        Filters=[
            {"Name": "name", "Values": ["al2023-ami-ecs-hvm-2023*-x86_64"]},
            {"Name": "state", "Values": ["available"]}
        ]
    )
    if not amis["Images"]:
        # Fallback to regular Amazon Linux 2023 AMI if ECS optimized not found
        logger.warning("ECS optimized AMI not found, falling back to regular Amazon Linux 2023")
        amis = ec2_client.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["al2023-ami-2023*-x86_64"]},
                {"Name": "state", "Values": ["available"]}
            ]
        )
        # Filter out minimal AMIs
        filtered_amis = [ami for ami in amis["Images"] if "minimal" not in ami["Name"].lower()]
        if not filtered_amis:
            filtered_amis = amis["Images"]
        latest_ami = sorted(filtered_amis, key=lambda x: x["CreationDate"], reverse=True)[0]
    else:
        latest_ami = sorted(amis["Images"], key=lambda x: x["CreationDate"], reverse=True)[0]
    
    ami_id = latest_ami["ImageId"]
    logger.debug(f"Using AMI: {ami_id} ({latest_ami['Name']})")
    
    # Prepare user data
    environment = {
        "projectName": project_name,
        "accountId": account_id,
        "region": region,
        "knowledge_base_id": knowledge_base_id,
        "data_source_id": data_source_id if data_source_id else "",
        "knowledge_base_role": knowledge_base_role_arn,
        "collectionArn": opensearch_info["arn"],
        "opensearch_url": opensearch_info["endpoint"],
        "vector_bucket_name": "",
        "vector_bucket_arn": "",
        "vector_index_name": vector_index_name,
        "vector_index_arn": "",
        "s3_bucket": s3_bucket_name,
        "s3_arn": f"arn:aws:s3:::{s3_bucket_name}",
        "sharing_url": f"https://{cloudfront_domain}"
    }
        
    user_data_script = get_setup_script(environment, git_name)
    
    # Get instance profile name
    instance_profile_name = f"instance-profile-{project_name}-{region}"
    
    # Validate VPC info and verify private subnets are available
    private_subnets = vpc_info.get("private_subnets", [])
    if not private_subnets:
        # Try to refresh subnet information from AWS
        logger.warning("  No private subnets in vpc_info, attempting to refresh from AWS...")
        try:
            vpc_id = vpc_info.get("vpc_id")
            if vpc_id:
                subnets_response = ec2_client.describe_subnets(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                )
                # Classify subnets and filter for available ones
                classified = classify_subnets(subnets_response["Subnets"], filter_available=True)
                private_subnets = classified["private_subnets"]
                
                if private_subnets:
                    logger.info(f"  Found {len(private_subnets)} available private subnet(s) after refresh")
                    vpc_info["private_subnets"] = private_subnets
        except Exception as e:
            logger.warning(f"  Failed to refresh subnet information: {e}")
    
    # Final validation
    if not private_subnets:
        raise ValueError(
            f"No private subnets found in VPC {vpc_info.get('vpc_id', 'unknown')}. "
            "Please ensure the VPC has at least one private subnet for EC2 deployment."
        )
    
    # Verify at least one subnet is available
    available_subnets = []
    for subnet_id in private_subnets:
        try:
            response = ec2_client.describe_subnets(SubnetIds=[subnet_id])
            if response["Subnets"] and response["Subnets"][0]["State"] == "available":
                available_subnets.append(subnet_id)
        except Exception as e:
            logger.warning(f"  Could not verify subnet {subnet_id}: {e}")
    
    if not available_subnets:
        # Wait a bit and retry
        logger.info("  Waiting for private subnets to become available...")
        time.sleep(10)
        for subnet_id in private_subnets:
            if wait_for_subnet_available(subnet_id, max_wait_time=60):
                available_subnets.append(subnet_id)
    
    if not available_subnets:
        raise ValueError(
            f"No available private subnets found in VPC {vpc_info.get('vpc_id', 'unknown')}. "
            "Please ensure the VPC has at least one available private subnet for EC2 deployment."
        )
    
    # Update vpc_info with available subnets
    vpc_info["private_subnets"] = available_subnets
    
    if not vpc_info.get("ecs_sg_id"):
        raise ValueError(
            f"No EC2 security group found in VPC {vpc_info.get('vpc_id', 'unknown')}. "
            "Please ensure the VPC has an EC2 security group."
        )
    
    # Create EC2 instance
    logger.debug(f"Launching EC2 instance: t3.medium in subnet {vpc_info['private_subnets'][0]}")
    response = ec2_client.run_instances(
        ImageId=ami_id,
        InstanceType="t3.medium",
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={"Name": instance_profile_name},
        UserData=base64.b64encode(user_data_script.encode('utf-8')).decode('utf-8'),
        NetworkInterfaces=[
            {
                "DeviceIndex": 0,
                "SubnetId": vpc_info["private_subnets"][0],
                "Groups": [vpc_info["ecs_sg_id"]],
                "AssociatePublicIpAddress": False,
                "DeleteOnTermination": True
            }
        ],
        BlockDeviceMappings=[
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": 80,
                    "DeleteOnTermination": True,
                    "Encrypted": True,
                    "VolumeType": "gp3"
                }
            }
        ],
        Monitoring={"Enabled": True},
        InstanceInitiatedShutdownBehavior="terminate",
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [{"Key": "Name", "Value": instance_name}]
            }
        ]
    )
    
    instance_id = response["Instances"][0]["InstanceId"]
    logger.info(f"✓ EC2 instance created: {instance_id}")
    logger.info(f"  Instance type: t3.medium")
    logger.info(f"  Deployed in private subnet: {vpc_info['private_subnets'][0]}")
    logger.info(f"  User data script configured for application deployment")
    
    return instance_id


def create_alb_target_group_and_listener(alb_info: Dict[str, str], instance_id: str, vpc_info: Dict[str, str]) -> Dict[str, str]:
    """Create ALB target group and listener."""
    logger.info("[9/10] Creating ALB target group and listener")
    
    target_port = 8501
    target_group_name = f"TG-for-{project_name}"
    
    # Check if target group already exists
    tg_arn = None
    try:
        tgs = elbv2_client.describe_target_groups(Names=[target_group_name])
        if tgs["TargetGroups"]:
            tg_arn = tgs["TargetGroups"][0]["TargetGroupArn"]
            logger.warning(f"  Target group already exists: {tg_arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "TargetGroupNotFound":
            logger.warning(f"  Error checking existing target group: {e}")
    
    # Create target group if it doesn't exist
    if not tg_arn:
        logger.debug(f"Creating target group on port {target_port}")
        try:
            tg_response = elbv2_client.create_target_group(
                Name=target_group_name,
                Protocol="HTTP",
                Port=target_port,
                VpcId=vpc_info["vpc_id"],
                HealthCheckProtocol="HTTP",
                HealthCheckPath="/",
                HealthCheckIntervalSeconds=30,
                HealthCheckTimeoutSeconds=5,
                HealthyThresholdCount=2,
                UnhealthyThresholdCount=3,
                TargetType="instance"
            )
            tg_arn = tg_response["TargetGroups"][0]["TargetGroupArn"]
            logger.debug(f"Target group created: {tg_arn}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "DuplicateTargetGroupName":
                # Try to get the existing target group again
                tgs = elbv2_client.describe_target_groups(Names=[target_group_name])
                if tgs["TargetGroups"]:
                    tg_arn = tgs["TargetGroups"][0]["TargetGroupArn"]
                    logger.warning(f"  Target group already exists: {tg_arn}")
            else:
                raise
    
    # Check if EC2 instance is already registered in target group
    instance_registered = False
    try:
        targets = elbv2_client.describe_target_health(TargetGroupArn=tg_arn)
        for target in targets.get("TargetHealthDescriptions", []):
            if target["Target"]["Id"] == instance_id and target["Target"]["Port"] == target_port:
                instance_registered = True
                logger.warning(f"  EC2 instance {instance_id} is already registered in target group")
                break
    except ClientError as e:
        logger.debug(f"  Error checking registered targets: {e}")
    
    # Register EC2 instance if not already registered
    if not instance_registered:
        logger.debug(f"Waiting for EC2 instance {instance_id} to be running...")
        waiter = ec2_client.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        
        logger.debug(f"Registering EC2 instance {instance_id} to target group")
        try:
            elbv2_client.register_targets(
                TargetGroupArn=tg_arn,
                Targets=[{"Id": instance_id, "Port": target_port}]
            )
            logger.info(f"  ✓ Registered EC2 instance {instance_id} to target group")
        except ClientError as e:
            if e.response["Error"]["Code"] == "DuplicateTarget":
                logger.warning(f"  EC2 instance {instance_id} is already registered in target group")
            else:
                raise
    
    origin_header_value = get_or_create_alb_origin_header(rotate=False)
    listener_arn = ensure_alb_listener_origin_protection(
        alb_info["arn"], tg_arn, origin_header_value
    )
    
    logger.info(f"✓ ALB target group and listener created")
    logger.info(f"  Target group: {tg_arn}")
    logger.info(f"  Listener: {listener_arn}")
    
    return {
        "target_group_arn": tg_arn,
        "listener_arn": listener_arn
    }


def run_setup_on_existing_instance(instance_id: Optional[str] = None):
    """Run setup script on existing EC2 instance via SSM."""
    instance_name = f"app-for-{project_name}"
    
    # Find instance if not provided
    if not instance_id:
        logger.info(f"Finding EC2 instance with name: {instance_name}")
        instances = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [instance_name]},
                {"Name": "instance-state-name", "Values": ["running"]}
            ]
        )
        
        found_instance = None
        for reservation in instances["Reservations"]:
            for instance in reservation["Instances"]:
                found_instance = instance["InstanceId"]
                break
        
        if not found_instance:
            raise Exception(f"No running EC2 instance found with name: {instance_name}")
        
        instance_id = found_instance
        logger.info(f"Found instance: {instance_id}")
    
    # Get infrastructure info from config or describe resources
    logger.info("Gathering infrastructure information...")
    
    # Try to read from config.json first
    config_path = "application/config.json"
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
            environment = {
                "projectName": config_data.get("projectName", project_name),
                "accountId": config_data.get("accountId", account_id),
                "region": config_data.get("region", region),
                "knowledge_base_id": config_data.get("knowledge_base_id", ""),
                "data_source_id": config_data.get("data_source_id", ""),
                "knowledge_base_role": config_data.get("knowledge_base_role", ""),
                "collectionArn": config_data.get("collectionArn", ""),
                "opensearch_url": config_data.get("opensearch_url", ""),
                "vector_bucket_name": config_data.get("vector_bucket_name", ""),
                "vector_bucket_arn": config_data.get("vector_bucket_arn", ""),
                "vector_index_name": config_data.get("vector_index_name", ""),
                "vector_index_arn": config_data.get("vector_index_arn", ""),
                "s3_bucket": config_data.get("s3_bucket", ""),
                "s3_arn": config_data.get("s3_arn", ""),
                "sharing_url": config_data.get("sharing_url", ""),
                "agentcore_memory_role": config_data.get("agentcore_memory_role", ""),
                "agentcore_websearch_gateway_name": config_data.get(
                    "agentcore_websearch_gateway_name", AGENTCORE_WEBSEARCH_GATEWAY_NAME
                ),
                "agentcore_websearch_gateway_region": config_data.get(
                    "agentcore_websearch_gateway_region", AGENTCORE_GATEWAY_REGION
                ),
                "agentcore_websearch_gateway_id": config_data.get(
                    "agentcore_websearch_gateway_id", ""
                ),
                "agentcore_websearch_gateway_url": config_data.get(
                    "agentcore_websearch_gateway_url", ""
                ),
                "agentcore_websearch_gateway_role": config_data.get(
                    "agentcore_websearch_gateway_role", ""
                ),
            }
            logger.info("Using configuration from config.json")
    except Exception as e:
        logger.warning(f"Could not read config.json: {e}")
        logger.info("Using default configuration")
        environment = {
            "projectName": project_name,
            "accountId": account_id,
            "region": region,
            "knowledge_base_id": "",
            "data_source_id": "",
            "knowledge_base_role": "",
            "collectionArn": "",
            "opensearch_url": "",
            "vector_bucket_name": "",
            "vector_bucket_arn": "",
            "vector_index_name": "",
            "vector_index_arn": "",
            "s3_bucket": "",
            "s3_arn": "",
            "sharing_url": "",
            "agentcore_memory_role": "",
            "agentcore_websearch_gateway_name": AGENTCORE_WEBSEARCH_GATEWAY_NAME,
            "agentcore_websearch_gateway_region": AGENTCORE_GATEWAY_REGION,
            "agentcore_websearch_gateway_id": "",
            "agentcore_websearch_gateway_url": "",
            "agentcore_websearch_gateway_role": "",
        }
    
    # Run setup script via SSM
    result = run_setup_script_via_ssm(instance_id, environment)
    
    logger.info("="*60)
    logger.info("Setup Script Execution Completed")
    logger.info("="*60)
    logger.info(f"Instance ID: {instance_id}")
    logger.info(f"Command ID: {result['command_id']}")
    logger.info(f"Status: {result['status']}")
    if result.get('output'):
        logger.info(f"Output: {result['output'][:500]}...")  # First 500 chars
    logger.info("="*60)
    
    return result


def verify_ec2_subnet_deployment():
    """Verify that existing EC2 instances are deployed in private subnets."""
    logger.info("Verifying EC2 subnet deployment...")
    
    instance_name = f"app-for-{project_name}"
    
    try:
        instances = ec2_client.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [instance_name]},
                {"Name": "instance-state-name", "Values": ["running", "pending", "stopping", "stopped"]}
            ]
        )
        
        for reservation in instances["Reservations"]:
            for instance in reservation["Instances"]:
                instance_id = instance["InstanceId"]
                subnet_id = instance["SubnetId"]
                has_public_ip = instance.get("PublicIpAddress") is not None
                
                # Check subnet type
                subnet_details = ec2_client.describe_subnets(SubnetIds=[subnet_id])
                subnet = subnet_details["Subnets"][0]
                
                # Determine if subnet is private or public
                is_private_subnet = False
                for tag in subnet.get("Tags", []):
                    if tag["Key"] == "aws-cdk:subnet-type" and tag["Value"] == "Private":
                        is_private_subnet = True
                        break
                
                # If no explicit tag, check route table for internet gateway
                if not is_private_subnet:
                    route_tables = ec2_client.describe_route_tables(
                        Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
                    )
                    for rt in route_tables["RouteTables"]:
                        for route in rt["Routes"]:
                            if route.get("GatewayId", "").startswith("igw-") and route.get("DestinationCidrBlock") == "0.0.0.0/0":
                                # This subnet has direct internet gateway route, so it's public
                                break
                        else:
                            continue
                        break
                    else:
                        # No direct internet gateway route found, likely private
                        is_private_subnet = True
                
                logger.info(f"  Instance {instance_id}:")
                logger.info(f"    Subnet: {subnet_id} ({subnet['CidrBlock']})")
                logger.info(f"    Subnet Type: {'Private' if is_private_subnet else 'Public'}")
                logger.info(f"    Has Public IP: {has_public_ip}")
                logger.info(f"    Private IP: {instance['PrivateIpAddress']}")
                
                if is_private_subnet and not has_public_ip:
                    logger.info(f"    ✓ Correctly deployed in private subnet")
                elif not is_private_subnet:
                    logger.warning(f"    WARNING: Instance is deployed in a PUBLIC subnet!")
                    logger.warning(f"    This is not recommended for production environments.")
                elif has_public_ip:
                    logger.warning(f"    WARNING: Instance has a public IP address!")
                
    except Exception as e:
        logger.debug(f"Could not verify EC2 deployment: {e}")

def _wait_for_s3files_status(
    describe_fn,
    resource_id_key: str,
    resource_id: str,
    ready_status: str = "available",
    max_wait_seconds: int = 600,
    poll_seconds: int = 10,
) -> bool:
    """Poll an S3 Files resource until it reaches the expected status."""
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        response = describe_fn(**{resource_id_key: resource_id})
        status = (response.get("status") or "").lower()
        if status == ready_status.lower():
            return True
        if status in {"error", "deleted"}:
            message = response.get("statusMessage", "")
            raise RuntimeError(
                f"S3 Files resource {resource_id} entered status {status}: {message}"
            )
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for S3 Files resource {resource_id}")


def _get_or_create_s3files_sync_role(s3_bucket_arn: str) -> str:
    """Create the IAM role S3 Files assumes to sync with the backing S3 bucket."""
    role_name = f"role-s3files-sync-for-{project_name}"
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowS3FilesAssumeRole",
                "Effect": "Allow",
                "Principal": {"Service": "elasticfilesystem.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:s3files:{region}:{account_id}:file-system/*"
                        )
                    },
                },
            }
        ],
    }
    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:GetBucketLocation",
                    "s3:GetBucketVersioning",
                    "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts",
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:GetObjectTagging",
                    "s3:GetObjectVersionTagging",
                    "s3:PutObject",
                    "s3:PutObjectTagging",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": [s3_bucket_arn, f"{s3_bucket_arn}/*"],
                "Condition": {
                    "StringEquals": {"aws:ResourceAccount": account_id}
                },
            }
        ],
    }
    eventbridge_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EventBridgeManage",
                "Effect": "Allow",
                "Action": [
                    "events:PutRule",
                    "events:PutTargets",
                    "events:DeleteRule",
                    "events:DisableRule",
                    "events:EnableRule",
                    "events:RemoveTargets",
                ],
                "Resource": "arn:aws:events:*:*:rule/DO-NOT-DELETE-S3-Files*",
                "Condition": {
                    "StringEquals": {
                        "events:ManagedBy": "elasticfilesystem.amazonaws.com"
                    }
                },
            },
            {
                "Sid": "EventBridgeRead",
                "Effect": "Allow",
                "Action": [
                    "events:DescribeRule",
                    "events:ListRules",
                    "events:ListRuleNamesByTarget",
                    "events:ListTargetsByRule",
                ],
                "Resource": "arn:aws:events:*:*:rule/*",
            },
        ],
    }

    created = False
    try:
        role = iam_client.get_role(RoleName=role_name)
        role_arn = role["Role"]["Arn"]
        iam_client.update_assume_role_policy(
            RoleName=role_name,
            PolicyDocument=json.dumps(trust_policy),
        )
        logger.info(f"  Reusing S3 Files sync role: {role_arn}")
    except iam_client.exceptions.NoSuchEntityException:
        role = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=f"S3 Files sync role for {project_name}",
        )
        role_arn = role["Role"]["Arn"]
        created = True
        logger.info(f"  Created S3 Files sync role: {role_arn}")

    for policy_name, policy_document in (
        ("s3-bucket-access", bucket_policy),
        ("eventbridge-sync", eventbridge_policy),
    ):
        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_document),
        )

    # Fresh IAM roles can take a few seconds to become assumable by S3 Files.
    if created:
        wait_seconds = 15
        logger.info(
            f"  Waiting {wait_seconds}s for IAM role propagation: {role_name}"
        )
        time.sleep(wait_seconds)

    return role_arn


def _normalize_s3files_prefix(prefix: str) -> str:
    if not prefix:
        return ""
    return prefix if prefix.endswith("/") else f"{prefix}/"


def _s3files_file_system_name_tag(item: Dict) -> str:
    for tag in item.get("tags") or []:
        if tag.get("key") == "Name" or tag.get("Key") == "Name":
            return tag.get("value") or tag.get("Value") or ""
    return item.get("name") or ""


def _find_s3files_file_system(
    s3_bucket_arn: str,
    prefix: str,
    name_tag: str,
) -> Optional[Dict[str, str]]:
    """Return FS matching bucket+prefix or Name tag (never reuse a different prefix)."""
    want_prefix = _normalize_s3files_prefix(prefix)
    paginator = s3files_client.get_paginator("list_file_systems")
    for page in paginator.paginate():
        for item in page.get("fileSystems", []):
            if item.get("bucket") != s3_bucket_arn:
                continue
            item_prefix = _normalize_s3files_prefix(item.get("prefix") or "")
            item_name = _s3files_file_system_name_tag(item)
            if item_prefix == want_prefix or item_name == name_tag:
                return {
                    "file_system_id": item.get("fileSystemId", ""),
                    "file_system_arn": item.get("fileSystemArn", ""),
                    "prefix": item_prefix,
                }
    return None


def _ensure_s3_bucket_versioning_enabled(s3_bucket_name: str) -> None:
    """Enable S3 bucket versioning (required for S3 Files file systems)."""
    response = s3_client.get_bucket_versioning(Bucket=s3_bucket_name)
    status = response.get("Status")
    if status == "Enabled":
        logger.info(f"  S3 bucket versioning already enabled: {s3_bucket_name}")
        return

    logger.info(f"  Enabling S3 bucket versioning for S3 Files: {s3_bucket_name}")
    s3_client.put_bucket_versioning(
        Bucket=s3_bucket_name,
        VersioningConfiguration={"Status": "Enabled"},
    )


def _get_or_create_s3files_file_system(
    s3_bucket_arn: str,
    role_arn: str,
    *,
    prefix: str,
    name_tag: str,
) -> Dict[str, str]:
    """Create or reuse an S3 Files file system for the given prefix/name."""
    existing = _find_s3files_file_system(s3_bucket_arn, prefix, name_tag)
    if existing and existing.get("file_system_id"):
        logger.info(
            f"  Reusing S3 Files file system: {existing['file_system_id']} "
            f"(prefix={existing.get('prefix') or prefix})"
        )
        return existing

    bucket = s3_bucket_arn.removeprefix("arn:aws:s3:::")
    _ensure_s3_bucket_versioning_enabled(bucket)

    normalized = _normalize_s3files_prefix(prefix)
    response = s3files_client.create_file_system(
        bucket=s3_bucket_arn,
        prefix=normalized,
        roleArn=role_arn,
        acceptBucketWarning=True,
        tags=[{"key": "Name", "value": name_tag}],
    )
    file_system_id = response["fileSystemId"]
    logger.info(f"  Created S3 Files file system: {file_system_id} (prefix={normalized})")
    _wait_for_s3files_status(
        s3files_client.get_file_system,
        "fileSystemId",
        file_system_id,
    )
    return {
        "file_system_id": file_system_id,
        "file_system_arn": response.get("fileSystemArn", ""),
        "prefix": normalized,
    }


def _ensure_security_group_nfs_access(client_sg_id: str, mount_sg_id: str) -> None:
    """Allow NFS (2049) between a client security group and S3 Files mount targets."""
    if not client_sg_id or not mount_sg_id:
        return

    try:
        ec2_client.authorize_security_group_egress(
            GroupId=client_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "UserIdGroupPairs": [{"GroupId": mount_sg_id}],
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            logger.warning(
                f"  Could not add NFS egress on client security group {client_sg_id}: {e}"
            )

    try:
        ec2_client.authorize_security_group_ingress(
            GroupId=mount_sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 2049,
                    "ToPort": 2049,
                    "UserIdGroupPairs": [{"GroupId": client_sg_id}],
                }
            ],
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            logger.warning(
                f"  Could not add NFS ingress on mount security group {mount_sg_id}: {e}"
            )


def _get_or_create_s3files_mount_security_group(
    vpc_id: str,
    client_sg_ids: List[str],
) -> str:
    """Security group for S3 Files mount targets (NFS 2049)."""
    group_name = f"s3files-mount-sg-for-{project_name}"
    unique_client_sg_ids = []
    for sg_id in client_sg_ids:
        if sg_id and sg_id not in unique_client_sg_ids:
            unique_client_sg_ids.append(sg_id)

    ingress_rules = [
        {
            "IpProtocol": "tcp",
            "FromPort": 2049,
            "ToPort": 2049,
            "UserIdGroupPairs": [{"GroupId": sg_id}],
        }
        for sg_id in unique_client_sg_ids
    ]
    sg_id = create_security_group(
        vpc_id=vpc_id,
        group_name=group_name,
        description=f"S3 Files mount target security group for {project_name}",
        ingress_rules=ingress_rules,
    )

    for client_sg_id in unique_client_sg_ids:
        _ensure_security_group_nfs_access(client_sg_id, sg_id)

    return sg_id


def ensure_ecs_task_s3files_policy(
    ecs_task_role_name: str,
    file_system_id: str,
    access_point_arn: str,
) -> None:
    """Grant ECS task role permissions to mount the app-data S3 Files volume only."""
    if not file_system_id or not access_point_arn:
        return

    file_system_arn = f"arn:aws:s3files:{region}:{account_id}:file-system/{file_system_id}"
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "S3FilesAppDataClientAccess",
                "Effect": "Allow",
                "Action": [
                    "s3files:ClientMount",
                    "s3files:ClientWrite",
                    "s3files:ClientRootAccess",
                ],
                "Resource": file_system_arn,
                "Condition": {
                    "ArnEquals": {
                        "s3files:AccessPointArn": access_point_arn,
                    }
                },
            },
            {
                "Sid": "S3FilesAppDataGetAccessPoint",
                "Effect": "Allow",
                "Action": ["s3files:GetAccessPoint"],
                "Resource": access_point_arn,
            },
            {
                "Sid": "S3FilesAppDataListMountTargets",
                "Effect": "Allow",
                "Action": ["s3files:ListMountTargets"],
                "Resource": file_system_arn,
            },
        ],
    }
    attach_inline_policy(
        ecs_task_role_name,
        f"s3files-ecs-task-policy-for-{project_name}",
        policy,
    )
    logger.info(
        f"  ✓ Attached app-data S3 Files policy to {ecs_task_role_name}"
    )


def _ensure_s3files_mount_targets(
    file_system_id: str,
    subnet_ids: List[str],
    security_group_ids: List[str],
) -> None:
    """Create mount targets in each private subnet that does not have one yet."""
    existing_subnets = set()
    paginator = s3files_client.get_paginator("list_mount_targets")
    for page in paginator.paginate(fileSystemId=file_system_id):
        for item in page.get("mountTargets", []):
            if item.get("subnetId"):
                existing_subnets.add(item["subnetId"])

    for subnet_id in subnet_ids:
        if subnet_id in existing_subnets:
            logger.info(f"  Reusing S3 Files mount target in subnet {subnet_id}")
            continue

        response = s3files_client.create_mount_target(
            fileSystemId=file_system_id,
            subnetId=subnet_id,
            securityGroups=security_group_ids,
        )
        mount_target_id = response.get("mountTargetId", subnet_id)
        logger.info(f"  Created S3 Files mount target {mount_target_id} in {subnet_id}")
        _wait_for_s3files_status(
            s3files_client.get_mount_target,
            "mountTargetId",
            mount_target_id,
        )


def _s3files_access_point_name_tag(item: Dict) -> str:
    for tag in item.get("tags") or []:
        if tag.get("key") == "Name" or tag.get("Key") == "Name":
            return tag.get("value") or tag.get("Value") or ""
    return item.get("name") or ""


def _get_or_create_s3files_access_point(
    file_system_id: str,
    *,
    name_tag: str,
) -> str:
    """Create or reuse an access point for the given file system / Name tag."""
    aps: List[Dict] = []
    paginator = s3files_client.get_paginator("list_access_points")
    for page in paginator.paginate(fileSystemId=file_system_id):
        aps.extend(page.get("accessPoints") or [])

    for item in aps:
        arn = item.get("accessPointArn")
        if not arn:
            continue
        if _s3files_access_point_name_tag(item) == name_tag or len(aps) == 1:
            logger.info(f"  Reusing S3 Files access point: {arn}")
            return arn

    response = s3files_client.create_access_point(
        fileSystemId=file_system_id,
        posixUser={"uid": 0, "gid": 0},
        rootDirectory={
            "path": "/",
            "creationPermissions": {
                "ownerUid": 0,
                "ownerGid": 0,
                "permissions": "0777",
            },
        },
        tags=[{"key": "Name", "value": name_tag}],
    )
    access_point_arn = response["accessPointArn"]
    logger.info(f"  Created S3 Files access point: {access_point_arn}")
    _wait_for_s3files_status(
        s3files_client.get_access_point,
        "accessPointId",
        response["accessPointId"],
    )
    return access_point_arn


def _iam_role_name_from_arn(role_arn: str) -> str:
    """Extract the IAM role name from a role ARN (last path segment)."""
    if ":role/" in role_arn:
        return role_arn.rsplit("/", 1)[-1]
    return role_arn


def _iam_role_exists(role_arn: str) -> bool:
    """Return True if the IAM role ARN resolves to an existing role."""
    if not role_arn:
        return False
    role_name = _iam_role_name_from_arn(role_arn)
    try:
        iam_client.get_role(RoleName=role_name)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return False
        logger.warning(f"  Could not verify IAM role {role_name}: {e}")
        return False


def _resolve_agent_runtime_role_arn() -> str:
    """Resolve AgentCore Runtime execution role ARN (config first, then naming)."""
    runtime_config = _load_runtime_agent_config("strands")
    configured = (runtime_config.get("agent_runtime_role") or "").strip()
    if configured:
        return configured

    runtime_project = (
        (runtime_config.get("projectName") or "").strip()
        or project_name
    )
    return (
        f"arn:aws:iam::{account_id}:role/"
        f"AmazonBedrockAgentCoreRuntimeRoleFor{runtime_project}"
    )


def _ensure_s3files_file_system_policy(
    file_system_id: str,
    access_point_arn: str,
    client_role_arns: List[str],
) -> None:
    """Allow only the given roles to mount/write via the access point."""
    principals = [arn for arn in client_role_arns if arn]
    if not principals:
        return

    # PutFileSystemPolicy rejects Principal ARNs for roles that do not exist yet
    # (same validation as S3 bucket policies).
    missing = [arn for arn in principals if not _iam_role_exists(arn)]
    if missing:
        for arn in missing:
            logger.info(
                "  Deferring S3 Files file system policy: IAM role not found yet (%s)",
                _iam_role_name_from_arn(arn),
            )
        return

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": principals if len(principals) > 1 else principals[0]},
                "Action": [
                    "s3files:ClientMount",
                    "s3files:ClientWrite",
                    "s3files:ClientRootAccess",
                ],
                "Condition": {
                    "StringEquals": {
                        "s3files:AccessPointArn": access_point_arn,
                    }
                },
            }
        ],
    }
    try:
        s3files_client.put_file_system_policy(
            fileSystemId=file_system_id,
            policy=json.dumps(policy),
        )
        logger.info(
            "  Applied S3 Files file system policy for %d principal(s)",
            len(principals),
        )
    except ClientError as e:
        logger.warning(f"  Could not apply S3 Files file system policy: {e}")


def _add_security_group_to_vpc_endpoint(endpoint_id: str, security_group_id: str) -> None:
    """Attach an additional security group to an interface VPC endpoint."""
    if not endpoint_id:
        return
    try:
        response = ec2_client.describe_vpc_endpoints(VpcEndpointIds=[endpoint_id])
        endpoints = response.get("VpcEndpoints", [])
        if not endpoints:
            return
        current_groups = endpoints[0].get("Groups", [])
        group_ids = [group["GroupId"] for group in current_groups]
        if security_group_id in group_ids:
            return
        group_ids.append(security_group_id)
        ec2_client.modify_vpc_endpoint(
            VpcEndpointId=endpoint_id,
            AddSecurityGroupIds=[security_group_id],
        )
        logger.info(f"  Added {security_group_id} to VPC endpoint {endpoint_id}")
    except ClientError as e:
        logger.warning(f"  Could not update VPC endpoint {endpoint_id}: {e}")


def _add_security_group_to_vpc_endpoint_by_service(
    vpc_id: str,
    service_name: str,
    security_group_id: str,
) -> None:
    """Attach a security group to an existing interface VPC endpoint by service name."""
    if not vpc_id or not security_group_id:
        return
    try:
        response = ec2_client.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "service-name", "Values": [service_name]},
            ]
        )
        endpoints = response.get("VpcEndpoints", [])
        if not endpoints:
            logger.debug(f"  VPC endpoint not found for {service_name} in {vpc_id}")
            return
        _add_security_group_to_vpc_endpoint(
            endpoints[0]["VpcEndpointId"],
            security_group_id,
        )
    except ClientError as e:
        logger.warning(
            f"  Could not attach {security_group_id} to VPC endpoint {service_name}: {e}"
        )


def _ensure_agent_runtime_vpc_endpoint_access(
    vpc_id: str,
    agent_runtime_sg_id: str,
) -> None:
    """Allow AgentCore runtime tasks to reach private-subnet interface VPC endpoints."""
    for service_name in (
        f"com.amazonaws.{region}.bedrock-runtime",
        f"com.amazonaws.{region}.bedrock-agentcore",
        f"com.amazonaws.{region}.bedrock-agentcore-control",
        f"com.amazonaws.{region}.secretsmanager",
    ):
        _add_security_group_to_vpc_endpoint_by_service(
            vpc_id,
            service_name,
            agent_runtime_sg_id,
        )


def create_s3_files_session_storage(
    vpc_info: Dict[str, str],
    s3_bucket_name: str,
) -> Dict[str, object]:
    """Provision S3 Files for AgentCore Runtime only (agentcore-sessions/).

    ECS must not mount this FS — app data lives on a separate app-data/ FS.
    """
    logger.info("[5.5/10] Creating S3 Files session storage (Runtime)")
    vpc_id = vpc_info["vpc_id"]
    private_subnets = vpc_info.get("private_subnets") or []
    if len(private_subnets) < 1:
        raise RuntimeError("At least one private subnet is required for S3 Files mount targets")

    s3_bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
    sync_role_arn = _get_or_create_s3files_sync_role(s3_bucket_arn)
    file_system = _get_or_create_s3files_file_system(
        s3_bucket_arn,
        sync_role_arn,
        prefix=S3_FILES_SESSION_PREFIX,
        name_tag=f"s3files-for-{project_name}",
    )
    file_system_id = file_system["file_system_id"]

    agent_runtime_sg_id = create_security_group(
        vpc_id=vpc_id,
        group_name=f"agent-runtime-sg-for-{project_name}",
        description=f"Security group for AgentCore Runtime ({project_name})",
    )
    s3files_mount_sg_id = _get_or_create_s3files_mount_security_group(
        vpc_id,
        [agent_runtime_sg_id],
    )
    _ensure_security_group_nfs_access(agent_runtime_sg_id, s3files_mount_sg_id)

    _ensure_s3files_mount_targets(
        file_system_id,
        private_subnets,
        [s3files_mount_sg_id],
    )
    access_point_arn = _get_or_create_s3files_access_point(
        file_system_id,
        name_tag=f"s3files-ap-for-{project_name}",
    )
    # Runtime IAM role is created later by runtime_agent/*/installer.py.
    # Apply FS policy now only on reinstall (role already exists); otherwise
    # prepare_s3files_for_runtime() runs after install_agent_runtime().
    prepare_s3files_for_runtime(
        {
            "file_system_id": file_system_id,
            "access_point_arn": access_point_arn,
        }
    )
    _ensure_agent_runtime_vpc_endpoint_access(
        vpc_info["vpc_id"],
        agent_runtime_sg_id,
    )

    logger.info("✓ S3 Files session storage ready")
    logger.info(f"  File system: {file_system_id}")
    logger.info(f"  Access point: {access_point_arn}")
    logger.info(f"  Mount path: {SESSION_STORAGE_MOUNT_PATH}")
    logger.info(f"  Prefix: {S3_FILES_SESSION_PREFIX}")
    logger.info(f"  Runtime subnets: {', '.join(private_subnets)}")
    logger.info(f"  Runtime security group: {agent_runtime_sg_id}")

    return {
        "file_system_id": file_system_id,
        "file_system_arn": file_system.get("file_system_arn", ""),
        "access_point_arn": access_point_arn,
        "mount_path": SESSION_STORAGE_MOUNT_PATH,
        "prefix": S3_FILES_SESSION_PREFIX,
        "subnets": private_subnets,
        "security_groups": [agent_runtime_sg_id],
        "mount_sg_id": s3files_mount_sg_id,
        "agent_runtime_sg_id": agent_runtime_sg_id,
    }


def _copy_s3_prefix_if_missing(
    bucket: str, src_prefix: str, dst_prefix: str
) -> int:
    """Copy src→dst objects when destination prefix is empty. Returns count."""
    dst_probe = s3_client.list_objects_v2(
        Bucket=bucket, Prefix=dst_prefix, MaxKeys=1
    )
    if dst_probe.get("KeyCount", 0) > 0:
        return 0
    copied = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=src_prefix):
        for obj in page.get("Contents") or []:
            src_key = obj.get("Key") or ""
            if not src_key.startswith(src_prefix):
                continue
            dst_key = dst_prefix + src_key[len(src_prefix) :]
            s3_client.copy_object(
                Bucket=bucket,
                Key=dst_key,
                CopySource={"Bucket": bucket, "Key": src_key},
            )
            copied += 1
    return copied


def _delete_s3_prefix(bucket: str, prefix: str) -> int:
    """Delete all objects under prefix. Returns deleted count."""
    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in (page.get("Contents") or []) if obj.get("Key")]
        if not keys:
            continue
        s3_client.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
        deleted += len(keys)
    return deleted


def _migrate_app_data_from_sessions(s3_bucket_name: str) -> None:
    """Copy ECS-owned data from agentcore-sessions/ into app-data/ (once).

    Migrates application-database/, litellm/, per-user graph/, settings.json.
    Skills/artifacts/checkpoints stay under agentcore-sessions.
    After a successful copy of sensitive prefixes, remove the session originals
    so Runtime NFS can no longer see tasks.db / virtual keys.
    """
    try:
        n = _copy_s3_prefix_if_missing(
            s3_bucket_name,
            f"{S3_FILES_SESSION_PREFIX}application-database/",
            f"{S3_FILES_APP_DATA_PREFIX}application-database/",
        )
        if n:
            logger.info(f"  Migrated {n} application-database object(s) → app-data/")
            removed = _delete_s3_prefix(
                s3_bucket_name, f"{S3_FILES_SESSION_PREFIX}application-database/"
            )
            if removed:
                logger.info(
                    f"  Removed {removed} application-database object(s) from agentcore-sessions/"
                )

        litellm_n = _copy_s3_prefix_if_missing(
            s3_bucket_name,
            f"{S3_FILES_SESSION_PREFIX}litellm/",
            f"{S3_FILES_APP_DATA_PREFIX}litellm/",
        )
        if litellm_n:
            logger.info(f"  Migrated {litellm_n} litellm object(s) → app-data/")
            removed = _delete_s3_prefix(
                s3_bucket_name, f"{S3_FILES_SESSION_PREFIX}litellm/"
            )
            if removed:
                logger.info(
                    f"  Removed {removed} litellm object(s) from agentcore-sessions/"
                )
        else:
            # Destination already populated from a prior run — still scrub session copy.
            dst = s3_client.list_objects_v2(
                Bucket=s3_bucket_name,
                Prefix=f"{S3_FILES_APP_DATA_PREFIX}litellm/",
                MaxKeys=1,
            )
            src = s3_client.list_objects_v2(
                Bucket=s3_bucket_name,
                Prefix=f"{S3_FILES_SESSION_PREFIX}litellm/",
                MaxKeys=1,
            )
            if dst.get("KeyCount", 0) > 0 and src.get("KeyCount", 0) > 0:
                removed = _delete_s3_prefix(
                    s3_bucket_name, f"{S3_FILES_SESSION_PREFIX}litellm/"
                )
                if removed:
                    logger.info(
                        f"  Removed {removed} leftover litellm object(s) from agentcore-sessions/"
                    )
            dst_db = s3_client.list_objects_v2(
                Bucket=s3_bucket_name,
                Prefix=f"{S3_FILES_APP_DATA_PREFIX}application-database/",
                MaxKeys=1,
            )
            src_db = s3_client.list_objects_v2(
                Bucket=s3_bucket_name,
                Prefix=f"{S3_FILES_SESSION_PREFIX}application-database/",
                MaxKeys=1,
            )
            if dst_db.get("KeyCount", 0) > 0 and src_db.get("KeyCount", 0) > 0:
                removed = _delete_s3_prefix(
                    s3_bucket_name,
                    f"{S3_FILES_SESSION_PREFIX}application-database/",
                )
                if removed:
                    logger.info(
                        f"  Removed {removed} leftover application-database "
                        f"object(s) from agentcore-sessions/"
                    )

        paginator = s3_client.get_paginator("list_objects_v2")
        users: List[str] = []
        for page in paginator.paginate(
            Bucket=s3_bucket_name,
            Prefix=S3_FILES_SESSION_PREFIX,
            Delimiter="/",
        ):
            for entry in page.get("CommonPrefixes") or []:
                child = (entry.get("Prefix") or "").rstrip("/")
                name = child.rsplit("/", 1)[-1] if child else ""
                if name and name not in ("application-database", "litellm", "checkpoints"):
                    users.append(name)

        graph_copied = 0
        settings_copied = 0
        for user in users:
            graph_copied += _copy_s3_prefix_if_missing(
                s3_bucket_name,
                f"{S3_FILES_SESSION_PREFIX}{user}/graph/",
                f"{S3_FILES_APP_DATA_PREFIX}{user}/graph/",
            )
            src_settings = f"{S3_FILES_SESSION_PREFIX}{user}/settings.json"
            dst_settings = f"{S3_FILES_APP_DATA_PREFIX}{user}/settings.json"
            try:
                s3_client.head_object(Bucket=s3_bucket_name, Key=dst_settings)
            except ClientError:
                try:
                    s3_client.head_object(Bucket=s3_bucket_name, Key=src_settings)
                except ClientError:
                    continue
                s3_client.copy_object(
                    Bucket=s3_bucket_name,
                    Key=dst_settings,
                    CopySource={"Bucket": s3_bucket_name, "Key": src_settings},
                )
                settings_copied += 1

        if graph_copied:
            logger.info(f"  Migrated {graph_copied} graph object(s) → app-data/")
        if settings_copied:
            logger.info(f"  Migrated {settings_copied} settings.json → app-data/")
    except ClientError as e:
        logger.warning(f"  app-data migration skipped: {e}")


def prepare_s3files_for_runtime(
    s3_files_info: Optional[Dict[str, object]] = None,
) -> bool:
    """Apply session S3 Files FS policy for the AgentCore Runtime role.

    Safe to call before the role exists (no-op + log) or after Runtime install.
    Returns True when the policy was applied (or already applicable principals).
    """
    info = dict(s3_files_info or {})
    file_system_id = str(info.get("file_system_id") or "").strip()
    access_point_arn = str(info.get("access_point_arn") or "").strip()

    if not file_system_id or not access_point_arn:
        app_config = _read_local_json_config(
            os.path.join(_project_root(), "application", "config.json")
        )
        runtime_config = _load_runtime_agent_config("strands")
        file_system_id = (
            file_system_id
            or str(app_config.get("s3_files_file_system_id") or "").strip()
            or str(runtime_config.get("s3_files_file_system_id") or "").strip()
        )
        access_point_arn = (
            access_point_arn
            or str(app_config.get("s3_files_access_point_arn") or "").strip()
            or str(runtime_config.get("s3_files_access_point_arn") or "").strip()
        )

    if not file_system_id or not access_point_arn:
        logger.warning(
            "  Skipping S3 Files session FS policy: missing file system / access point"
        )
        return False

    role_arn = _resolve_agent_runtime_role_arn()
    if not _iam_role_exists(role_arn):
        logger.info(
            "  S3 Files session FS policy deferred until AgentCore Runtime role exists "
            f"({_iam_role_name_from_arn(role_arn)})"
        )
        return False

    _ensure_s3files_file_system_policy(
        file_system_id,
        access_point_arn,
        [role_arn],
    )
    logger.info("  ✓ Applied S3 Files session FS policy for AgentCore Runtime")
    return True


def prepare_s3files_for_ecs(
    vpc_info: Dict[str, str],
    app_data_info: Dict[str, object],
    ecs_task_role_name: str,
) -> None:
    """Grant ECS mount access to the dedicated app-data S3 Files FS only."""
    file_system_id = str(app_data_info.get("file_system_id") or "")
    access_point_arn = str(app_data_info.get("access_point_arn") or "")
    if not file_system_id or not access_point_arn:
        logger.warning("  Skipping S3 Files ECS prep: missing app-data FS/AP")
        return

    ecs_sg_id = vpc_info.get("ecs_sg_id") or ""
    mount_sg_id = str(app_data_info.get("mount_sg_id") or "")
    if ecs_sg_id and mount_sg_id:
        _ensure_security_group_nfs_access(ecs_sg_id, mount_sg_id)

    ecs_task_role_arn = (
        f"arn:aws:iam::{account_id}:role/{ecs_task_role_name}"
        if ecs_task_role_name
        else ""
    )
    if ecs_task_role_arn:
        _ensure_s3files_file_system_policy(
            file_system_id,
            access_point_arn,
            [ecs_task_role_arn],
        )
        logger.info("  ✓ Applied S3 Files app-data FS policy for ECS only")

    ensure_ecs_task_s3files_policy(
        ecs_task_role_name,
        file_system_id,
        access_point_arn,
    )


def create_s3_files_app_data_storage(
    vpc_info: Dict[str, str],
    s3_bucket_name: str,
    *,
    mount_sg_id: str = "",
    ecs_sg_id: str = "",
    ecs_task_role_name: str = "",
) -> Dict[str, object]:
    """Provision a separate S3 Files FS for ECS (tasks.db / graph / settings / litellm)."""
    logger.info("[5.6/10] Creating S3 Files app-data storage (ECS)")
    vpc_id = vpc_info["vpc_id"]
    private_subnets = vpc_info.get("private_subnets") or []
    if len(private_subnets) < 1:
        raise RuntimeError("At least one private subnet is required for S3 Files mount targets")

    s3_bucket_arn = f"arn:aws:s3:::{s3_bucket_name}"
    sync_role_arn = _get_or_create_s3files_sync_role(s3_bucket_arn)
    file_system = _get_or_create_s3files_file_system(
        s3_bucket_arn,
        sync_role_arn,
        prefix=S3_FILES_APP_DATA_PREFIX,
        name_tag=f"s3files-app-data-for-{project_name}",
    )
    file_system_id = file_system["file_system_id"]

    if not mount_sg_id:
        client_sg_ids = [ecs_sg_id] if ecs_sg_id else []
        mount_sg_id = _get_or_create_s3files_mount_security_group(
            vpc_id,
            client_sg_ids,
        )
    if ecs_sg_id and mount_sg_id:
        _ensure_security_group_nfs_access(ecs_sg_id, mount_sg_id)

    _ensure_s3files_mount_targets(
        file_system_id,
        private_subnets,
        [mount_sg_id],
    )
    access_point_arn = _get_or_create_s3files_access_point(
        file_system_id,
        name_tag=f"s3files-ap-app-data-for-{project_name}",
    )
    _migrate_app_data_from_sessions(s3_bucket_name)

    app_data_info: Dict[str, object] = {
        "file_system_id": file_system_id,
        "file_system_arn": file_system.get("file_system_arn", ""),
        "access_point_arn": access_point_arn,
        "mount_path": APP_DATA_MOUNT_PATH,
        "prefix": S3_FILES_APP_DATA_PREFIX,
        "mount_sg_id": mount_sg_id,
        "subnets": private_subnets,
    }

    if ecs_task_role_name:
        prepare_s3files_for_ecs(vpc_info, app_data_info, ecs_task_role_name)

    logger.info("✓ S3 Files app-data storage ready")
    logger.info(f"  File system: {file_system_id}")
    logger.info(f"  Access point: {access_point_arn}")
    logger.info(f"  Mount path: {APP_DATA_MOUNT_PATH}")
    logger.info(f"  Prefix: {S3_FILES_APP_DATA_PREFIX}")
    return app_data_info


def apply_s3_files_config(
    app_config: Dict[str, object],
    s3_files_info: Optional[Dict[str, object]],
    s3_files_app_data_info: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Attach S3 Files session + app-data settings to application config."""
    if s3_files_info:
        app_config["s3_files_file_system_id"] = s3_files_info.get("file_system_id", "")
        app_config["s3_files_access_point_arn"] = s3_files_info.get(
            "access_point_arn", ""
        )
        app_config["s3_files_mount_path"] = s3_files_info.get(
            "mount_path", SESSION_STORAGE_MOUNT_PATH
        )
        app_config["agent_runtime_vpc_subnets"] = s3_files_info.get("subnets", [])
        app_config["agent_runtime_security_groups"] = s3_files_info.get(
            "security_groups", []
        )
    if s3_files_app_data_info:
        app_config["s3_files_app_data_file_system_id"] = s3_files_app_data_info.get(
            "file_system_id", ""
        )
        app_config["s3_files_app_data_access_point_arn"] = s3_files_app_data_info.get(
            "access_point_arn", ""
        )
        app_config["s3_files_app_data_mount_path"] = s3_files_app_data_info.get(
            "mount_path", APP_DATA_MOUNT_PATH
        )
    return app_config


def install_agent_runtime(runtime_type: str = "strands") -> bool:
    """Install Agent Runtime by running the appropriate installer.py script."""
    logger.info(f"[11/10] Installing Agent Runtime: {runtime_type}")
    runtime_config = _load_runtime_agent_config(runtime_type)
    runtime_project_name = runtime_config.get("projectName", git_name)
    logger.info(f"  Agent runtime name: {agent_runtime_name(runtime_project_name)}")
    
    # Determine installer path based on runtime type
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if runtime_type == "strands":
        installer_path = os.path.join(script_dir, "runtime_agent", "strands", "installer.py")
    else:
        logger.error(f"Unknown Agent Runtime type: {runtime_type}")
        return False
    
    if not os.path.exists(installer_path):
        logger.error(f"Installer not found: {installer_path}")
        return False
    
    try:
        logger.info(f"Running installer: {installer_path}")
        result = subprocess.run(
            [sys.executable, installer_path],
            cwd=os.path.dirname(installer_path),
            check=True,
            capture_output=False
        )
        logger.info(f"✓ Agent Runtime ({runtime_type}) installation completed")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to install Agent Runtime ({runtime_type}): {e}")
        return False
    except Exception as e:
        logger.error(f"Error installing Agent Runtime ({runtime_type}): {e}")
        return False


def check_application_ready(domain: str, max_attempts: int = 120, wait_seconds: int = 10) -> None:
    """Check if the application is ready by making HTTP requests to the CloudFront domain.
    
    Args:
        domain: CloudFront domain name
        max_attempts: Maximum number of attempts to check readiness
        wait_seconds: Seconds to wait between attempts
    """
    logger.info(f"[10/10] Checking if application is ready at https://{domain}")
    logger.info(f"  Maximum {max_attempts} attempts, {wait_seconds} seconds between attempts (up to {max_attempts * wait_seconds // 60} minutes)")
    url = f"https://{domain}"
    
    start_time = time.time()
    last_info_time = start_time
    info_interval = 30  # Output progress every 30 seconds
    
    for attempt in range(max_attempts):
        current_attempt = attempt + 1
        progress_percent = (current_attempt / max_attempts) * 100
        elapsed_time = time.time() - start_time
        
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0')
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.getcode() == 200:
                    elapsed_minutes = elapsed_time / 60
                    logger.info(f"✓ Application is ready! Status code: {response.getcode()}")
                    logger.info(f"  Total attempts: {current_attempt}/{max_attempts}, elapsed time: {elapsed_minutes:.1f} minutes")
                    return
        except urllib.error.HTTPError as e:
            # HTTP errors like 502, 503 are expected during deployment
            if e.code in [502, 503, 504]:
                current_time = time.time()
                # Output at info level every 30 seconds, or on first attempt, or during last 10 attempts
                if (current_time - last_info_time >= info_interval or 
                    current_attempt == 1 or 
                    current_attempt > max_attempts - 10):
                    logger.info(f"  In progress... [{current_attempt}/{max_attempts}] - HTTP {e.code} response")
                    last_info_time = current_time
                else:
                    logger.debug(f"Application not ready yet (attempt {current_attempt}/{max_attempts}): HTTP {e.code}")
            else:
                # Other HTTP errors might indicate the app is responding but with an error
                elapsed_minutes = elapsed_time / 60
                logger.info(f"Application responded with HTTP {e.code}, considering it ready")
                logger.info(f"  Total attempts: {current_attempt}/{max_attempts}, elapsed time: {elapsed_minutes:.1f} minutes")
                return
        except (urllib.error.URLError, OSError, Exception) as e:
            current_time = time.time()
            # Output at info level every 30 seconds, or on first attempt, or during last 10 attempts
            if (current_time - last_info_time >= info_interval or 
                current_attempt == 1 or 
                current_attempt > max_attempts - 10):
                error_msg = str(e)[:100]  # Limit error message length
                logger.info(f"  In progress... [{current_attempt}/{max_attempts}] - Connection attempt")
                logger.debug(f"  Detailed error: {error_msg}")
                last_info_time = current_time
            else:
                logger.debug(f"Application not ready yet (attempt {current_attempt*10}/{max_attempts*10}): {e}")
        
        if attempt < max_attempts - 1:
            time.sleep(wait_seconds)
        else:
            elapsed_minutes = elapsed_time / 60
            logger.warning(f"Application readiness check timed out after {max_attempts * wait_seconds} seconds ({elapsed_minutes:.1f} minutes)")
            logger.warning(f"  Total attempts: {max_attempts}/{max_attempts} (100%)")
            logger.warning("The application may still be deploying. Please check manually.")



def main():
    """Main function to create all infrastructure."""
    parser = argparse.ArgumentParser(description="AWS Infrastructure Installer")
    parser.add_argument(
        "--run-setup",
        metavar="INSTANCE_ID",
        nargs="?",
        const="",
        help="(Legacy EC2) Run setup script on existing EC2 instance via SSM.",
    )
    parser.add_argument(
        "--schedule-only",
        action="store_true",
        help="Deploy only my-schedule infra (DynamoDB, Lambda, Scheduler) and update config.",
    )
    parser.add_argument(
        "--verify-deployment",
        action="store_true",
        help="(Legacy EC2) Verify EC2 instances are deployed in private subnets.",
    )
    parser.add_argument(
        "--skip-docker-build",
        action="store_true",
        help="Skip local Docker build/push and reuse the latest image tag in ECR.",
    )
    parser.add_argument(
        "--install-agent-runtime",
        metavar="RUNTIME_TYPE",
        nargs="?",
        const="strands",
        help="Install Agent Runtime. RUNTIME_TYPE can be 'strands' (default)."
    )
    
    args = parser.parse_args()
    
    # If --run-setup flag is provided, run setup script via SSM
    if args.run_setup is not None:
        instance_id = args.run_setup if args.run_setup else None
        run_setup_on_existing_instance(instance_id)
        return
    
    # If --verify-deployment flag is provided, verify EC2 subnet deployment

    if getattr(args, "schedule_only", False):
        cfg = {}
        try:
            cfg = load_application_config()
        except Exception:
            pass
        sharing = (cfg.get("sharing_url") or "").strip()
        if not sharing:
            raise SystemExit(
                "--schedule-only requires application/config.json sharing_url"
            )
        ecs_task_role_name = f"role-ecs-task-for-{project_name}-{region}"
        schedule_info = deploy_schedule_infrastructure(
            app_base_url=sharing,
            ecs_task_role_name=ecs_task_role_name,
        )
        merged = dict(cfg)
        merged.update(schedule_info)
        write_application_config(merged)
        logger.info("✓ my-schedule infrastructure deployed (--schedule-only)")
        return

    if args.verify_deployment:
        verify_ec2_subnet_deployment()
        return
    
    # If --install-agent-runtime flag is provided, install Agent Runtime
    if args.install_agent_runtime is not None:
        runtime_type = args.install_agent_runtime if args.install_agent_runtime else "strands"
        success = install_agent_runtime(runtime_type)
        if success:
            # Runtime role now exists — attach session S3 Files FS policy if FS is ready.
            prepare_s3files_for_runtime()
        sys.exit(0 if success else 1)
    
    ensure_boto3_supports_agentcore_connector()

    logger.info("="*60)
    logger.info("Starting AWS Infrastructure Deployment")
    logger.info("="*60)
    logger.info(f"Project: {project_name}")
    logger.info(f"Region: {region}")
    logger.info(f"Account ID: {account_id}")
    logger.info(f"Bucket Name: {bucket_name}")
    logger.info("="*60)
    
    start_time = time.time()

    s3_bucket_name = None
    knowledge_base_role_arn = None
    opensearch_info = None
    knowledge_base_id = None
    data_source_id = None
    vpc_info = None
    alb_info = None
    cloudfront_info = None
    ecs_info = None
    app_environment = None
    agentcore_websearch_gateway_info = None
    agentcore_memory_role_arn = None
    memory_id = None
    s3_files_info = None
    s3_files_app_data_info = None
    cognito_info = None
    deployment_success = False
    
    try:
        # 1. Create S3 bucket
        s3_bucket_name = create_s3_bucket()
        logger.info(f"S3 bucket created...")
        
        # 2. Create IAM roles
        knowledge_base_role_arn = create_knowledge_base_role()
        ecs_roles = create_ecs_roles()
        agentcore_memory_role_arn = create_agentcore_memory_role()
        memory_id = create_agentcore_memory(agentcore_memory_role_arn)
        agentcore_websearch_gateway_role_arn = create_agentcore_websearch_gateway_role()
        agentcore_websearch_gateway_info = get_or_create_agentcore_websearch_gateway(
            agentcore_websearch_gateway_role_arn
        )
        logger.info(f"IAM roles created...")

        # 2.5. Cognito User Pool + admin (Web UI id/password auth)
        cognito_info = create_cognito_user_pool()
        logger.info("Cognito User Pool created...")
        
        # 3. Create OpenSearch Serverless collection
        opensearch_info = create_opensearch_collection(
            knowledge_base_role_arn=knowledge_base_role_arn
        )
        logger.info("OpenSearch collection created...")
        
        # 4. Create Knowledge Base with OpenSearch
        knowledge_base_id, data_source_id = create_knowledge_base_with_opensearch(
            opensearch_info, knowledge_base_role_arn, s3_bucket_name
        )
        logger.info(f"Knowledge base created...")
        
        # 5. Create VPC
        vpc_info = create_vpc()
        logger.info(f"VPC created...")

        # 5.5. S3 Files session storage (Runtime / agentcore-sessions/)
        ecs_task_role_name = f"role-ecs-task-for-{project_name}-{region}"
        s3_files_info = create_s3_files_session_storage(vpc_info, s3_bucket_name)
        logger.info("S3 Files session storage created...")

        # 5.6. S3 Files app-data storage (ECS / app-data/)
        s3_files_app_data_info = create_s3_files_app_data_storage(
            vpc_info,
            s3_bucket_name,
            mount_sg_id=str(s3_files_info.get("mount_sg_id") or ""),
            ecs_sg_id=vpc_info.get("ecs_sg_id", ""),
            ecs_task_role_name=ecs_task_role_name,
        )
        logger.info("S3 Files app-data storage created...")

        # 5.7. CloudFront→ALB origin verification header (Secrets Manager)
        logger.info("Creating/loading ALB origin header secret...")
        origin_header_value = get_or_create_alb_origin_header()
        
        # 6. Create ALB
        alb_info = create_alb(vpc_info)
        logger.info(f"ALB created...")
        
        # 7. Create CloudFront distribution
        cloudfront_info = create_cloudfront_distribution(
            alb_info, s3_bucket_name, origin_header_value
        )
        logger.info(f"CloudFront distribution created...")

        # my-schedule: DynamoDB + Lambda + EventBridge Scheduler
        schedule_info = deploy_schedule_infrastructure(
            app_base_url=f"https://{cloudfront_info['domain']}",
            ecs_task_role_name=ecs_task_role_name,
        )
        
        # 8. Build and push Docker image to ECR, then deploy ECS service
        sync_application_capability_lists()
        app_environment = build_app_environment(
            knowledge_base_role_arn,
            opensearch_info,
            s3_bucket_name,
            cloudfront_info["domain"],
            knowledge_base_id,
            data_source_id,
            agentcore_websearch_gateway_info,
            cognito_info=cognito_info,
            agentcore_memory_role_arn=agentcore_memory_role_arn,
            memory_id=memory_id,
        )
        app_environment = apply_s3_files_config(
            app_environment, s3_files_info, s3_files_app_data_info
        )
        app_environment.update(schedule_info)
        if write_application_config(app_environment):
            logger.info("Local testing is available while deployment continues:")
            logger.info("  uvicorn application.server:app --host 0.0.0.0 --port 8501")

        # Install AgentCore runtime after CloudFront so config.json gets sharing_url
        if install_agent_runtime("strands"):
            logger.info("Strands agent runtime installed...")
            # Runtime installer writes agent_runtime_arn; merge into ECS env before deploy
            app_environment = _merge_runtime_agent_settings(app_environment)
            if write_application_config(app_environment):
                logger.info(
                    "✓ Merged agent_runtime_arn into application config for ECS: "
                    f"{app_environment.get('agent_runtime_arn', '')}"
                )
            # Runtime IAM role now exists — apply session S3 Files FS policy.
            if s3_files_info:
                prepare_s3files_for_runtime(s3_files_info)
            # Refresh ECS task role so AgentCore Resource ARNs include the real runtime
            # (e.g. strands_runtime-*), not only root project_name.
            ecs_roles = create_ecs_roles()
            logger.info("ECS task AgentCore IAM policy refreshed for installed runtime...")
            # Re-attach app-data S3 Files IAM after role refresh.
            if s3_files_app_data_info:
                prepare_s3files_for_ecs(
                    vpc_info, s3_files_app_data_info, ecs_task_role_name
                )
        else:
            logger.warning("Strands agent runtime installation failed or was skipped.")
            if s3_files_info:
                logger.warning(
                    "  S3 Files session FS policy was not applied "
                    "(AgentCore Runtime role missing). Re-run with "
                    "--install-agent-runtime after fixing Runtime install."
                )

        repository_uri = create_ecr_repository()
        image_build_tag = None
        if args.skip_docker_build:
            image_uri = resolve_ecr_image_uri(repository_uri)
            image_build_tag = image_uri.rsplit(":", 1)[-1]
            app_environment["latest_image_tag"] = image_build_tag
            app_environment["build_number"] = image_build_tag
            logger.warning(f"Skipping Docker build; using existing image: {image_uri}")
        else:
            image_uri, image_build_tag = build_and_push_docker_image(repository_uri)
            app_environment["latest_image_tag"] = image_build_tag
            app_environment["build_number"] = image_build_tag
            if write_application_config(app_environment):
                logger.info(f"✓ Updated {_application_config_path()} with build number: {image_build_tag}")
        log_group_name = create_ecs_log_group()
        ecs_info = deploy_ecs_service(
            vpc_info,
            alb_info,
            ecs_roles,
            image_uri,
            app_environment,
            log_group_name,
            s3_files_info=s3_files_app_data_info,
            origin_header_value=origin_header_value,
        )
        logger.info("ECS service deployed...")
        
        # Check whether the application is ready
        logger.info(f"Checking if application is ready: {cloudfront_info['domain']}")
        check_application_ready(cloudfront_info["domain"])        
        
        deployment_success = True
        
        # Output summary
        elapsed_time = time.time() - start_time
        logger.info("")
        logger.info("="*60)
        logger.info("Infrastructure Deployment Completed Successfully!")
        logger.info("="*60)
        logger.info("Summary:")
        logger.info(f"  S3 Bucket: {s3_bucket_name}")
        logger.info(f"  VPC ID: {vpc_info['vpc_id']}")
        logger.info(f"  Public Subnets: {', '.join(vpc_info['public_subnets'])}")
        logger.info(f"  Private Subnets: {', '.join(vpc_info['private_subnets'])}")
        logger.info(f"  ALB DNS: http://{alb_info['dns']}/")
        logger.info(f"  CloudFront Domain: https://{cloudfront_info['domain']}")
        logger.info(f"  ECS Service: {ecs_info['service_name']} (Fargate in private subnet)")
        logger.info(f"  ECR Image: {image_uri}")
        if image_build_tag:
            logger.info(f"  Build Number: {image_build_tag}")
        logger.info(f"  OpenSearch Endpoint: {opensearch_info['endpoint']}")
        logger.info(f"  OpenSearch Collection ARN: {opensearch_info['arn']}")
        logger.info(f"  Knowledge Base ID: {knowledge_base_id}")
        logger.info(f"  Knowledge Base Role: {knowledge_base_role_arn}")
        if agentcore_memory_role_arn:
            logger.info(f"  AgentCore Memory Role: {agentcore_memory_role_arn}")
        if memory_id:
            logger.info(f"  AgentCore Memory ID: {memory_id}")
        if agentcore_websearch_gateway_info:
            logger.info(
                f"  AgentCore Web Search Gateway: "
                f"{agentcore_websearch_gateway_info.get('gateway_name')} "
                f"({agentcore_websearch_gateway_info.get('gateway_id')})"
            )
            logger.info(
                f"  AgentCore Web Search Gateway URL: "
                f"{agentcore_websearch_gateway_info.get('gateway_url')}"
            )
        if cognito_info:
            logger.info(f"  Cognito User Pool: {cognito_info.get('cognito_user_pool_id')} ({cognito_info.get('cognito_user_pool_name')})")
            logger.info(f"  Cognito Client ID: {cognito_info.get('cognito_client_id')}")
            logger.info(f"  Cognito Admin: {cognito_info.get('cognito_admin_username')}")
        if s3_files_info:
            logger.info(f"  S3 Files Access Point: {s3_files_info.get('access_point_arn')}")
            logger.info(
                f"  Agent Runtime Subnets: {', '.join(s3_files_info.get('subnets', []))}"
            )
        if s3_files_app_data_info:
            logger.info(
                f"  S3 Files (ECS app-data) AP: "
                f"{s3_files_app_data_info.get('access_point_arn')}"
            )
            logger.info(
                f"  S3 Files (ECS app-data) Mount: "
                f"{s3_files_app_data_info.get('mount_path')} "
                f"(prefix=app-data/)"
            )
        logger.info("")
        logger.info(f"Total deployment time: {elapsed_time/60:.2f} minutes")
        logger.info("="*60)
        logger.info("Note: ECS service deployment and CloudFront distribution may take 15-20 minutes to fully deploy")
        logger.info("="*60)
        
        logger.info(f"OpenSearch Endpoint: {opensearch_info['endpoint']}")
        logger.info(f"OpenSearch Collection ARN: {opensearch_info['arn']}")
        
        logger.info("="*60)
        logger.info("")
        logger.info("="*60)
        logger.info("  IMPORTANT: CloudFront Domain Address")
        logger.info("="*60)
        logger.info(f" CloudFront URL: https://{cloudfront_info['domain']}")
        logger.info("")
        logger.info("Note: CloudFront distribution and ECS service may take 15-20 minutes to fully deploy")
        logger.info("      Once deployed, you can access your application at the URL above")
        logger.info(f"      Login with Cognito username '{COGNITO_ADMIN_USERNAME}' and the password set during install")
        logger.info("="*60)
        logger.info("")
        
    except Exception as e:
        elapsed_time = time.time() - start_time
        logger.error("")
        logger.error("="*60)
        logger.error("Deployment Failed!")
        logger.error("="*60)
        logger.error(f"Error: {e}")
        logger.error(f"Deployment time before failure: {elapsed_time/60:.2f} minutes")
        logger.error("="*60)
        import traceback
        logger.error(traceback.format_exc())
        raise
    finally:
        config_path = _application_config_path()
        if app_environment is not None:
            config_data = app_environment
            if s3_files_info or s3_files_app_data_info:
                config_data = apply_s3_files_config(
                    config_data, s3_files_info, s3_files_app_data_info
                )
        else:
            config_data = build_config_from_deployment_state(
                knowledge_base_id=knowledge_base_id,
                data_source_id=data_source_id,
                knowledge_base_role_arn=knowledge_base_role_arn,
                opensearch_info=opensearch_info,
                s3_bucket_name=s3_bucket_name,
                cloudfront_info=cloudfront_info,
                s3_files_info=s3_files_info,
                s3_files_app_data_info=s3_files_app_data_info,
                cognito_info=cognito_info,
                agentcore_memory_role_arn=agentcore_memory_role_arn,
                memory_id=memory_id,
            )

        if opensearch_info:
            logger.info(f"OpenSearch Endpoint: {opensearch_info.get('endpoint', 'N/A')}")
            logger.info(f"OpenSearch Collection ARN: {opensearch_info.get('arn', 'N/A')}")

        if write_application_config(config_data):
            if deployment_success:
                logger.info(f"✓ Updated {config_path}")
            else:
                logger.info(f"✓ Saved partial deployment info to {config_path}")


if __name__ == "__main__":
    main()

