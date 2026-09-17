#!/usr/bin/env python3
"""
Unified installation script
Sequentially executes: IAM policy creation -> Bedrock Guardrail creation ->
Docker image build and ECR push -> AgentCore runtime creation/update
All functionality integrated into a single file
"""

import subprocess
import sys
import os
import json
import shutil
import argparse
import time
from datetime import datetime
from typing import Optional
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")

def update_config(key, value):
    """Update config.json with a key-value pair."""
    try:
        config = load_config()
        config[key] = value
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        print(f"Error updating config: {e}")
        return False


def _repo_root() -> str:
    return os.path.normpath(os.path.join(script_dir, "..", ".."))


def runtime_build_context() -> str:
    """Repo root (Dockerfile copies runtime_agent/strands + graph/lib)."""
    return _repo_root()


def runtime_dockerfile() -> str:
    return os.path.join(runtime_build_context(), "runtime_agent", "strands", "Dockerfile")


def get_knowledge_base_name() -> str:
    """Return project_name from repo root installer.py (Bedrock Knowledge Base name)."""
    root_installer_path = os.path.join(_repo_root(), "installer.py")
    try:
        with open(root_installer_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("project_name = "):
                    value = stripped.split("=", 1)[1].strip()
                    if "#" in value:
                        value = value.split("#", 1)[0].strip()
                    return value.strip('"').strip("'")
    except OSError as e:
        print(f"Warning: Could not read root installer.py: {e}")

    app_config_path = os.path.join(_repo_root(), "application", "config.json")
    try:
        with open(app_config_path, "r", encoding="utf-8") as f:
            app_config = json.load(f)
            project_name = app_config.get("projectName")
            if project_name:
                return project_name
    except (OSError, json.JSONDecodeError):
        pass

    return "strands-runtime"


def get_root_installer_region() -> str:
    """Return region from repo root installer.py."""
    root_installer_path = os.path.join(_repo_root(), "installer.py")
    try:
        with open(root_installer_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("region = "):
                    value = stripped.split("=", 1)[1].strip()
                    if "#" in value:
                        value = value.split("#", 1)[0].strip()
                    return value.strip('"').strip("'")
    except OSError as e:
        print(f"Warning: Could not read root installer.py: {e}")
    return ""


def _load_application_config() -> dict:
    """Load application/config.json when available."""
    app_config_path = os.path.join(_repo_root(), "application", "config.json")
    try:
        with open(app_config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _session_region() -> str:
    """Return AWS region from the active boto3 session."""
    return boto3.Session().region_name or ""


def _resolve_region(config: dict) -> str:
    region = config.get("region") or ""
    if region:
        return region

    region = _session_region()
    if region:
        return region

    app_config = _load_application_config()
    region = app_config.get("region") or ""
    if region:
        return region

    region = get_root_installer_region()
    if region:
        return region

    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-west-2"


def _create_config_from_session() -> dict:
    """Initialize config fields from boto3 session when config.json is missing."""
    region = _resolve_region({})
    config = {
        "region": region,
        "projectName": get_knowledge_base_name(),
    }

    try:
        sts = boto3.client("sts", region_name=region)
        config["accountId"] = sts.get_caller_identity()["Account"]
    except Exception as e:
        print(f"Warning: Could not resolve account ID from STS: {e}")

    print(
        f"  ✓ config.json initialized from boto3 session "
        f"(region={config.get('region')}, projectName={config.get('projectName')})"
    )
    return config


def load_config():
    """Load config.json and ensure required fields are populated."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        print("config.json not found, initializing from boto3 session...")
        config = _create_config_from_session()
    except Exception as e:
        print(f"Failed to parse config.json file: {e}")
        config = {}

    return _ensure_config_defaults(config)


def _resolve_account_id(config: dict) -> str:
    account_id = config.get("accountId") or ""
    if account_id:
        return account_id

    app_config = _load_application_config()
    account_id = app_config.get("accountId") or ""
    if account_id:
        return account_id

    try:
        sts = boto3.client("sts")
        return sts.get_caller_identity()["Account"]
    except Exception:
        return ""


def _resolve_project_name(config: dict) -> str:
    project_name = config.get("projectName") or ""
    if project_name:
        return project_name

    app_config = _load_application_config()
    project_name = app_config.get("projectName") or ""
    if project_name:
        return project_name

    return get_knowledge_base_name()


def _ensure_config_defaults(config: dict) -> dict:
    """Fill missing required config fields and persist updates."""
    updated = dict(config)
    changed = False

    region = _resolve_region(updated)
    if updated.get("region") != region:
        updated["region"] = region
        changed = True

    account_id = _resolve_account_id(updated)
    if account_id and updated.get("accountId") != account_id:
        updated["accountId"] = account_id
        changed = True

    project_name = _resolve_project_name(updated)
    if updated.get("projectName") != project_name:
        updated["projectName"] = project_name
        changed = True

    app_config = _load_application_config()
    for key in (
        "s3_files_access_point_arn",
        "s3_files_file_system_id",
        "agent_runtime_vpc_subnets",
        "agent_runtime_security_groups",
        "memory_id",
        "agentcore_memory_role",
    ):
        app_value = app_config.get(key)
        if app_value and updated.get(key) != app_value:
            updated[key] = app_value
            changed = True

    if changed:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2)
        print(
            "  ✓ config.json defaults applied: "
            f"region={updated.get('region')}, projectName={updated.get('projectName')}"
        )

    return updated


def _parse_s3_vectors_names(vector_bucket_arn: str, index_arn: str) -> dict:
    """Derive vector bucket/index names from S3 Vectors ARNs (legacy)."""
    vector_bucket_name = ""
    vector_index_name = ""
    if vector_bucket_arn and "/bucket/" in vector_bucket_arn:
        vector_bucket_name = vector_bucket_arn.split("/bucket/", 1)[1].split("/")[0]
    if index_arn and "/index/" in index_arn:
        vector_index_name = index_arn.rsplit("/index/", 1)[1]
    return {
        "vector_bucket_name": vector_bucket_name,
        "vector_index_name": vector_index_name,
    }


def _opensearch_endpoint_from_collection_arn(collection_arn: str, region: str) -> str:
    """Best-effort OpenSearch Serverless endpoint from collection ARN."""
    # arn:aws:aoss:region:account:collection/collection-id
    if not collection_arn or ":collection/" not in collection_arn:
        return ""
    collection_id = collection_arn.rsplit(":collection/", 1)[1]
    if not collection_id:
        return ""
    return f"https://{collection_id}.{region}.aoss.amazonaws.com"


def _find_data_source_id(bedrock_agent_client, knowledge_base_id: str, s3_bucket_name: str = "") -> str:
    """Return matching data source ID for the Knowledge Base."""
    try:
        response = bedrock_agent_client.list_data_sources(
            knowledgeBaseId=knowledge_base_id,
            maxResults=100,
        )
        data_sources = response.get("dataSourceSummaries", [])
        if not data_sources:
            return ""

        if s3_bucket_name:
            for data_source in data_sources:
                if data_source.get("name") == s3_bucket_name:
                    return data_source.get("dataSourceId", "")

        return data_sources[0].get("dataSourceId", "")
    except ClientError as e:
        print(f"Warning: Could not list data sources: {e}")
        return ""


def update_knowledge_base_config() -> bool:
    """Look up Knowledge Base by root installer project_name and update config.json."""
    print(f"\n{'='*60}")
    print("Updating Knowledge Base configuration")
    print(f"{'='*60}")

    try:
        config = load_config()
        region = config.get("region")
        if not region:
            print("Error: region not found in config.json")
            return False

        knowledge_base_name = get_knowledge_base_name()
        print(f"Knowledge Base name (from root installer.py): {knowledge_base_name}")

        app_config = _load_application_config()
        bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
        kb_list = bedrock_agent_client.list_knowledge_bases()
        knowledge_base_id = None
        for kb in kb_list.get("knowledgeBaseSummaries", []):
            if kb.get("name") == knowledge_base_name:
                knowledge_base_id = kb.get("knowledgeBaseId")
                break

        if not knowledge_base_id:
            print(f"Warning: Knowledge Base '{knowledge_base_name}' not found in {region}")
            updates = {
                "knowledge_base_name": knowledge_base_name,
                "collectionArn": "",
                "opensearch_url": "",
            }
            for key in (
                "knowledge_base_id",
                "knowledge_base_role",
                "data_source_id",
                "s3_bucket",
                "s3_arn",
                "sharing_url",
                "collectionArn",
                "opensearch_url",
                "vector_bucket_name",
                "vector_bucket_arn",
                "vector_index_name",
                "vector_index_arn",
                "s3_files_access_point_arn",
                "s3_files_file_system_id",
                "agent_runtime_vpc_subnets",
                "agent_runtime_security_groups",
                "agentcore_memory_role",
                "memory_id",
            ):
                if app_config.get(key):
                    updates[key] = app_config[key]
            config.update(updates)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            if updates.get("knowledge_base_id"):
                print(f"✓ config.json updated from application/config.json")
                print(f"  - knowledge_base_id: {updates['knowledge_base_id']}")
            else:
                print("✓ config.json updated with knowledge_base_name only")
            return True

        kb_details = bedrock_agent_client.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
        knowledge_base = kb_details.get("knowledgeBase", {})

        updates = {
            "knowledge_base_name": knowledge_base_name,
            "knowledge_base_id": knowledge_base_id,
            "knowledge_base_role": knowledge_base.get("roleArn", ""),
            "collectionArn": "",
            "opensearch_url": "",
            "vector_bucket_name": "",
            "vector_bucket_arn": "",
            "vector_index_name": "",
            "vector_index_arn": "",
        }

        storage = knowledge_base.get("storageConfiguration", {})
        storage_type = storage.get("type", "")
        if storage_type == "OPENSEARCH_SERVERLESS":
            opensearch_cfg = storage.get("opensearchServerlessConfiguration", {})
            collection_arn = opensearch_cfg.get("collectionArn", "")
            updates["collectionArn"] = collection_arn
            updates["vector_index_name"] = opensearch_cfg.get("vectorIndexName", "")
            updates["opensearch_url"] = (
                app_config.get("opensearch_url")
                or _opensearch_endpoint_from_collection_arn(collection_arn, region)
            )
        elif storage_type == "S3_VECTORS":
            s3_vectors_cfg = storage.get("s3VectorsConfiguration", {})
            vector_bucket_arn = s3_vectors_cfg.get("vectorBucketArn", "")
            vector_index_arn = s3_vectors_cfg.get("indexArn", "")
            parsed_names = _parse_s3_vectors_names(vector_bucket_arn, vector_index_arn)
            updates.update(
                {
                    "vector_bucket_arn": vector_bucket_arn,
                    "vector_index_arn": vector_index_arn,
                    "vector_bucket_name": parsed_names["vector_bucket_name"],
                    "vector_index_name": parsed_names["vector_index_name"],
                }
            )

        for key in (
            "s3_bucket",
            "s3_arn",
            "sharing_url",
            "data_source_id",
            "collectionArn",
            "opensearch_url",
            "vector_bucket_name",
            "vector_bucket_arn",
            "vector_index_name",
            "vector_index_arn",
            "s3_files_access_point_arn",
            "s3_files_file_system_id",
            "agent_runtime_vpc_subnets",
            "agent_runtime_security_groups",
            "agentcore_memory_role",
            "memory_id",
        ):
            if app_config.get(key) and not updates.get(key):
                updates[key] = app_config[key]

        s3_bucket_name = updates.get("s3_bucket") or app_config.get("s3_bucket", "")
        if not updates.get("data_source_id"):
            updates["data_source_id"] = _find_data_source_id(
                bedrock_agent_client,
                knowledge_base_id,
                s3_bucket_name,
            )

        config.update(updates)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        print(f"✓ config.json updated for Knowledge Base: {knowledge_base_name}")
        print(f"  - knowledge_base_id: {updates.get('knowledge_base_id', '')}")
        if updates.get("data_source_id"):
            print(f"  - data_source_id: {updates['data_source_id']}")
        if updates.get("collectionArn"):
            print(f"  - collectionArn: {updates['collectionArn']}")
        if updates.get("opensearch_url"):
            print(f"  - opensearch_url: {updates['opensearch_url']}")
        if updates.get("vector_index_arn"):
            print(f"  - vector_index_arn: {updates['vector_index_arn']}")
        return True
    except Exception as e:
        print(f"Error updating Knowledge Base configuration: {e}")
        return False

# ============================================================================
# IAM Policy and Role Creation Functions
# ============================================================================

def agent_runtime_name(project_name: str) -> str:
    """Return Bedrock AgentCore runtime name (e.g. strands_runtime)."""
    return project_name.replace("-", "_")


def _project_agent_runtime_resource_arns(config) -> list:
    """IAM Resource ARNs limited to this project's AgentCore runtime (+ endpoints)."""
    region = config["region"]
    account_id = config["accountId"]
    project_name = config.get("projectName", "agentcore")
    runtime_name = agent_runtime_name(project_name)
    arns = [
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
    agent_runtime_arn = (config.get("agent_runtime_arn") or "").strip()
    if agent_runtime_arn:
        if agent_runtime_arn not in arns:
            arns.append(agent_runtime_arn)
        endpoint_arn = f"{agent_runtime_arn}/runtime-endpoint/*"
        if endpoint_arn not in arns:
            arns.append(endpoint_arn)
    return arns


# Runtime tools only touch CF-shared prefixes (upload/read artifacts, images, docs).
# App data (tasks.db, graph, settings) lives under app-data/ on a separate S3 Files FS.
RUNTIME_S3_OBJECT_PREFIXES = ("artifacts/", "images/", "docs/")
RUNTIME_S3_DENY_OBJECT_PREFIXES = ("app-data/", "agentcore-sessions/")


def _project_s3_resource_arns(config) -> tuple:
    """Return (bucket_arns, object_arns, list_prefixes, deny_object_arns)."""
    region = config["region"]
    account_id = config["accountId"]
    project_name = config.get("projectName", "agentcore")
    s3_bucket = config.get(
        "s3_bucket",
        f"storage-for-{project_name}-{account_id}-{region}",
    )
    s3_arn = (config.get("s3_arn") or "").strip()
    if s3_arn.startswith("arn:aws:s3:::"):
        named = s3_arn[len("arn:aws:s3:::"):].split("/", 1)[0]
        if named:
            s3_bucket = named

    bucket_arn = f"arn:aws:s3:::{s3_bucket}"
    object_arns = [
        f"{bucket_arn}/{prefix}*" for prefix in RUNTIME_S3_OBJECT_PREFIXES
    ]
    list_prefixes: list[str] = []
    for prefix in RUNTIME_S3_OBJECT_PREFIXES:
        trimmed = prefix.rstrip("/")
        list_prefixes.extend([trimmed, f"{trimmed}/*"])
    deny_object_arns = [
        f"{bucket_arn}/{prefix}*" for prefix in RUNTIME_S3_DENY_OBJECT_PREFIXES
    ]
    return [bucket_arn], object_arns, list_prefixes, deny_object_arns


def _project_secret_resource_arns(config) -> list:
    """Secrets Manager ARNs used by this project (Tavily only; no Cognito stubs)."""
    region = config["region"]
    account_id = config["accountId"]
    project_name = config.get("projectName", "agentcore")
    secret_arns = [
        f"arn:aws:secretsmanager:{region}:{account_id}:secret:tavilyapikey-{project_name}*",
        f"arn:aws:secretsmanager:{region}:{account_id}:secret:{project_name}/schedule-agent-token*",
    ]
    kb_name = (config.get("knowledge_base_name") or "").strip()
    if kb_name and kb_name != project_name:
        secret_arns.append(
            f"arn:aws:secretsmanager:{region}:{account_id}:secret:tavilyapikey-{kb_name}*"
        )
    return secret_arns


def create_bedrock_agentcore_policy(config):
    """Create IAM policy for Bedrock AgentCore access"""
    region = config['region']
    accountId = config['accountId']
    projectName = config.get('projectName', 'agentcore')
    gateway_region = config.get(
        "agentcore_websearch_gateway_region",
        config.get("agentcore_gateway_region", "us-east-1"),
    )
    runtime_resource_arns = _project_agent_runtime_resource_arns(config)
    bucket_arns, object_arns, list_prefixes, deny_object_arns = (
        _project_s3_resource_arns(config)
    )
    secret_arns = _project_secret_resource_arns(config)
    
    policy_name = f"AmazonBedrockAgentCoreRuntimePolicyFor{projectName}"
    policy_description = f"Policy for accessing Bedrock AgentCore Runtime endpoints"
    
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WorkloadAccessToken",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                "Resource": [
                    (
                        f"arn:aws:bedrock-agentcore:{region}:{accountId}:"
                        f"workload-identity-directory/default/workload-identity/*"
                    ),
                ],
            },
            {
                "Sid": "InvokeAgentCoreGatewayAndWebSearch",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:InvokeGateway",
                    "bedrock-agentcore:InvokeWebSearch",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{gateway_region}:{accountId}:gateway/*",
                    (
                        f"arn:aws:bedrock-agentcore:{gateway_region}:"
                        f"aws:tool/web-search.v1"
                    ),
                ],
            },
            {
                # ListGateways is account-scoped; IAM requires Resource "*".
                "Sid": "ListAgentCoreGateways",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:ListGateways",
                    "bedrock-agentcore-control:ListGateways",
                ],
                "Resource": ["*"],
            },

            {
                # ListAgentRuntimes is account-scoped; IAM requires Resource "*".
                "Sid": "ListAgentRuntimes",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:ListAgentRuntimes",
                    "bedrock-agentcore-control:ListAgentRuntimes",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "GetAndInvokeAgentRuntime",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore-control:GetAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeWithWebResponse",
                ],
                "Resource": runtime_resource_arns,
            },
            {
                "Sid": "GetAgentCoreGateway",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore-control:GetGateway",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{gateway_region}:{accountId}:gateway/*",
                ],
            },
            {
                # Runtime calls Memory data/control APIs for save (CreateEvent) and
                # recall_memory (Retrieve/List/GetMemoryRecords, GetMemory strategies).
                "Sid": "AgentCoreMemoryAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:DeleteEvent",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                    "bedrock-agentcore:ListActors",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:GetMemory",
                    "bedrock-agentcore:UpdateMemory",
                    "bedrock-agentcore:ListMemories",
                    "bedrock-agentcore-control:GetMemory",
                    "bedrock-agentcore-control:UpdateMemory",
                    "bedrock-agentcore-control:ListMemories",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:{region}:{accountId}:memory/*",
                    f"arn:aws:bedrock-agentcore:{region}:{accountId}:memory/{projectName.replace('-', '_')}*",
                ],
            },
            {
                "Sid": "BedrockModelInvoke",
                "Effect": "Allow",
                "Action": [
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:GetInferenceProfile",
                    "bedrock:GetFoundationModel",
                    "bedrock:ApplyGuardrail",
                    "bedrock:GetGuardrail",
                    "bedrock:Retrieve",
                    "bedrock:RetrieveAndGenerate",
                ],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{accountId}:inference-profile/*",
                    f"arn:aws:bedrock:{region}:{accountId}:guardrail/*",
                    f"arn:aws:bedrock:{region}:{accountId}:guardrail-profile/*",
                    f"arn:aws:bedrock:{region}:{accountId}:knowledge-base/*",
                ],
            },
            {
                "Sid": "BedrockListRead",
                "Effect": "Allow",
                "Action": [
                    "bedrock:ListGuardrails",
                    "bedrock:ListKnowledgeBases",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "BedrockMantleAccess",
                "Effect": "Allow",
                "Action": [
                    "bedrock-mantle:Get*",
                    "bedrock-mantle:List*",
                    "bedrock-mantle:CreateInference"
                ],
                # OpenAI Mantle models (e.g. gpt-5.5) call us-east-1/2 even when
                # the AgentCore runtime itself runs in config['region'].
                "Resource": [
                    f"arn:aws:bedrock-mantle:*:{accountId}:project/*"
                ]
            },
            {
                "Sid": "BedrockMantleBearerToken",
                "Effect": "Allow",
                "Action": [
                    "bedrock-mantle:CallWithBearerToken"
                ],
                "Resource": "*"
            },
            {
                "Sid": "SecretsManagerRead",
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                "Resource": secret_arns,
            },
            {
                "Sid": "ECRImagePull",
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:DescribeRepositories",
                    "ecr:ListImages",
                    "ecr:DescribeImages"
                ],
                "Resource": "*"
            },
            {
                "Sid": "LogsAccess",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams"
                ],
                "Resource": [
                    f"arn:aws:logs:{region}:{accountId}:log-group:/aws/bedrock-agentcore/*",
                    f"arn:aws:logs:{region}:{accountId}:log-group:/aws/bedrock-agentcore/*:log-stream:*"
                ]
            },
            {
                "Sid": "CloudWatchMetricsAndXRay",
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:PutMetricData",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:PutAttributes",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                "Resource": "*"
            },
            {
                "Sid": "ProjectS3BucketMeta",
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketLocation",
                ],
                "Resource": bucket_arns,
            },
            {
                "Sid": "ProjectS3ListAllowedPrefixes",
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                ],
                "Resource": bucket_arns,
                "Condition": {
                    "StringLike": {
                        "s3:prefix": list_prefixes,
                    }
                },
            },
            {
                "Sid": "ProjectS3Objects",
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                ],
                "Resource": object_arns,
            },
            {
                "Sid": "DenySensitiveS3Prefixes",
                "Effect": "Deny",
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                ],
                "Resource": deny_object_arns,
            },
            {
                "Sid": "VpcNetworkInterface",
                "Effect": "Allow",
                "Action": [
                    "ec2:CreateNetworkInterface",
                    "ec2:CreateNetworkInterfacePermission",
                    "ec2:DeleteNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DescribeSubnets",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DescribeVpcs",
                    "ec2:AssignPrivateIpAddresses",
                    "ec2:UnassignPrivateIpAddresses",
                ],
                "Resource": "*"
            }
        ]
    }

    file_system_id = config.get("s3_files_file_system_id")
    access_point_arn = config.get("s3_files_access_point_arn")
    if file_system_id and access_point_arn:
        file_system_arn = (
            f"arn:aws:s3files:{region}:{accountId}:file-system/{file_system_id}"
        )
        policy_document["Statement"].extend(
            [
                {
                    "Sid": "S3FilesClientAccess",
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
                    "Sid": "S3FilesGetAccessPoint",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:GetAccessPoint",
                    ],
                    "Resource": access_point_arn,
                },
                {
                    "Sid": "S3FilesListMountTargets",
                    "Effect": "Allow",
                    "Action": [
                        "s3files:ListMountTargets",
                    ],
                    "Resource": file_system_arn,
                },
            ]
        )
    
    try:
        iam_client = boto3.client('iam')
        
        # Check if policy already exists
        try:
            existing_policy = iam_client.get_policy(PolicyArn=f"arn:aws:iam::{accountId}:policy/{policy_name}")
            print(f"Existing policy found: {existing_policy['Policy']['Arn']}")
            
            # List all policy versions
            versions_response = iam_client.list_policy_versions(PolicyArn=existing_policy['Policy']['Arn'])
            versions = versions_response['Versions']
            
            # If we have 5 versions, delete the oldest non-default version
            if len(versions) >= 5:
                print(f"Policy has {len(versions)} versions, cleaning up old versions...")
                
                # Find non-default versions to delete
                non_default_versions = [v for v in versions if not v['IsDefaultVersion']]
                
                if non_default_versions:
                    # Delete the oldest non-default version
                    oldest_version = non_default_versions[0]
                    iam_client.delete_policy_version(
                        PolicyArn=existing_policy['Policy']['Arn'],
                        VersionId=oldest_version['VersionId']
                    )
                    print(f"✓ Deleted old policy version: {oldest_version['VersionId']}")
                else:
                    # If all versions are default, we need to set a different version as default first
                    for version in versions[1:]:  # Skip the current default
                        try:
                            iam_client.set_default_policy_version(
                                PolicyArn=existing_policy['Policy']['Arn'],
                                VersionId=version['VersionId']
                            )
                            # Now delete the old default
                            iam_client.delete_policy_version(
                                PolicyArn=existing_policy['Policy']['Arn'],
                                VersionId=versions[0]['VersionId']
                            )
                            print(f"✓ Switched default version and deleted old version: {versions[0]['VersionId']}")
                            break
                        except Exception as e:
                            print(f"Failed to switch version {version['VersionId']}: {e}")
                            continue
            
            # Create policy version
            response = iam_client.create_policy_version(
                PolicyArn=existing_policy['Policy']['Arn'],
                PolicyDocument=json.dumps(policy_document),
                SetAsDefault=True
            )
            print(f"✓ Policy update completed: {response['PolicyVersion']['VersionId']}")
            return existing_policy['Policy']['Arn']
            
        except iam_client.exceptions.NoSuchEntityException:
            # Create new policy
            response = iam_client.create_policy(
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_document),
                Description=policy_description
            )
            print(f"✓ New policy created: {response['Policy']['Arn']}")
            return response['Policy']['Arn']
            
    except Exception as e:
        print(f"Policy creation failed: {e}")
        return None

def attach_policy_to_role(role_name, policy_arn):
    """Attach policy to IAM role"""
    try:
        iam_client = boto3.client('iam')
        
        # Attach policy to role
        response = iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn=policy_arn
        )
        print(f"✓ Policy attached successfully: {policy_arn}")
        return True
        
    except Exception as e:
        print(f"Policy attachment failed: {e}")
        return False

def create_trust_policy_for_bedrock(config):
    """Trust policy for AgentCore Runtime: service principal only + confused-deputy guards.

    Does not trust account root. SourceArn is limited to this project's runtime name.
    """
    account_id = config["accountId"]
    region = config["region"]
    project_name = config.get("projectName", "agentcore")
    runtime_name = agent_runtime_name(project_name)
    source_arns = [
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_name}",
        f"arn:aws:bedrock-agentcore:{region}:{account_id}:runtime/{runtime_name}-*",
    ]
    agent_runtime_arn = (config.get("agent_runtime_arn") or "").strip()
    if agent_runtime_arn and agent_runtime_arn not in source_arns:
        source_arns.append(agent_runtime_arn)

    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {
                    "Service": "bedrock-agentcore.amazonaws.com"
                },
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {
                        "aws:SourceAccount": account_id,
                    },
                    "ArnLike": {
                        "aws:SourceArn": source_arns,
                    },
                },
            }
        ],
    }

def create_bedrock_agentcore_role(config):
    """Create IAM role for Bedrock AgentCore MCP access"""
    projectName = config.get('projectName', 'agentcore')
    role_name = f"AmazonBedrockAgentCoreRuntimeRoleFor{projectName}"
    policy_arn = create_bedrock_agentcore_policy(config)
    
    if not policy_arn:
        print("Role creation aborted due to policy creation failure")
        return None
    
    try:
        iam_client = boto3.client('iam')
        
        # Check if role already exists
        try:
            existing_role = iam_client.get_role(RoleName=role_name)
            print(f"Existing role found: {existing_role['Role']['Arn']}")
            
            # Update trust policy
            trust_policy = create_trust_policy_for_bedrock(config)
            iam_client.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(trust_policy)
            )
            print("✓ Trust policy updated successfully")
            
            # Attach policy
            attach_policy_to_role(role_name, policy_arn)
            
            return existing_role['Role']['Arn']
            
        except iam_client.exceptions.NoSuchEntityException:
            # Create new role
            trust_policy = create_trust_policy_for_bedrock(config)
            
            response = iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Role for Bedrock AgentCore MCP access"
            )
            print(f"✓ New role created: {response['Role']['Arn']}")
            
            # Attach policy
            attach_policy_to_role(role_name, policy_arn)
            
            return response['Role']['Arn']
            
    except Exception as e:
        print(f"Role creation failed: {e}")
        return None

def create_iam_policies():
    """Create IAM policies and roles"""
    print(f"\n{'='*60}")
    print("Creating IAM policies and roles")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        
        # Create Bedrock AgentCore policy
        print("\n1. Creating Bedrock AgentCore policy...")
        policy_arn = create_bedrock_agentcore_policy(config)
        
        # Create Bedrock AgentCore role
        print("\n2. Creating Bedrock AgentCore role...")
        role_arn = create_bedrock_agentcore_role(config)
        
        if not role_arn:
            print("Role creation failed")
            return False
        
        # Update AgentCore configuration
        print("\n3. Updating AgentCore configuration...")
        update_config('agent_runtime_role', role_arn)
        print(f"✓ AgentCore configuration updated: {role_arn}")
        
        print("\n✓ IAM policies and roles creation completed")
        return True
        
    except Exception as e:
        print(f"Error creating IAM policies: {e}")
        return False

# ============================================================================
# Bedrock Guardrail Creation Functions
# ============================================================================

def guardrail_name(project_name: str) -> str:
    """Return Bedrock Guardrail name for the project."""
    safe_name = project_name.replace("_", "-").lower()
    return f"guardrail-for-{safe_name}"


def _bedrock_guardrail_content_policy() -> dict:
    """Content filters: block sexual content and prompt attacks (jailbreak/injection)."""
    return {
        "filtersConfig": [
            {
                "type": "SEXUAL",
                "inputStrength": "HIGH",
                "outputStrength": "HIGH",
                "inputAction": "BLOCK",
                "outputAction": "BLOCK",
                "inputModalities": ["TEXT"],
                "outputModalities": ["TEXT"],
            },
            {
                "type": "PROMPT_ATTACK",
                "inputStrength": "HIGH",
                "outputStrength": "NONE",
                "inputAction": "BLOCK",
                "outputAction": "NONE",
                "inputModalities": ["TEXT"],
            },
        ]
    }


def _find_guardrail_by_name(bedrock_client, name: str) -> dict | None:
    """Return guardrail summary matching name, or None."""
    next_token = None
    while True:
        kwargs = {"maxResults": 100}
        if next_token:
            kwargs["nextToken"] = next_token
        response = bedrock_client.list_guardrails(**kwargs)
        for guardrail in response.get("guardrails", []):
            if guardrail.get("name") == name:
                return guardrail
        next_token = response.get("nextToken")
        if not next_token:
            break
    return None


def create_bedrock_guardrail() -> bool:
    """Create or update Amazon Bedrock Guardrail for input safety."""
    print(f"\n{'='*60}")
    print("Creating/updating Amazon Bedrock Guardrail")
    print(f"{'='*60}")

    try:
        config = load_config()
        region = config.get("region")
        project_name = config.get("projectName", "strands-runtime")
        if not region:
            print("Error: region not found in config.json")
            return False

        name = guardrail_name(project_name)
        description = (
            f"Content safety guardrail for {project_name}: "
            "blocks sexual content and prompt attacks in user input."
        )
        blocked_input_message = (
            "요청이 안전 정책에 의해 차단되었습니다. "
            "성적 표현 또는 프롬프트 공격이 감지되었습니다."
        )
        blocked_output_message = (
            "응답이 안전 정책에 의해 차단되었습니다."
        )
        content_policy = _bedrock_guardrail_content_policy()

        bedrock_client = boto3.client("bedrock", region_name=region)
        existing = _find_guardrail_by_name(bedrock_client, name)

        if existing:
            guardrail_id = existing["id"]
            print(f"Existing guardrail found: {name} ({guardrail_id})")
            response = bedrock_client.update_guardrail(
                guardrailIdentifier=guardrail_id,
                name=name,
                description=description,
                contentPolicyConfig=content_policy,
                blockedInputMessaging=blocked_input_message,
                blockedOutputsMessaging=blocked_output_message,
            )
            print(f"✓ Guardrail updated: version {response.get('version', 'DRAFT')}")
        else:
            print(f"Creating guardrail: {name}")
            response = bedrock_client.create_guardrail(
                name=name,
                description=description,
                contentPolicyConfig=content_policy,
                blockedInputMessaging=blocked_input_message,
                blockedOutputsMessaging=blocked_output_message,
            )
            guardrail_id = response["guardrailId"]
            print(f"✓ Guardrail created: {guardrail_id}")

        guardrail_arn = response.get("guardrailArn") or existing.get("arn", "")
        guardrail_version = "DRAFT"

        update_config("guardrail_id", guardrail_id)
        update_config("guardrail_version", guardrail_version)
        update_config("guardrail_arn", guardrail_arn)
        update_config("guardrail_name", name)

        print(f"✓ Guardrail configuration saved to config.json")
        print(f"  - guardrail_id: {guardrail_id}")
        print(f"  - guardrail_version: {guardrail_version}")
        print(f"  - guardrail_name: {name}")
        print("  - filters: SEXUAL (HIGH), PROMPT_ATTACK (HIGH)")
        return True
    except ClientError as e:
        print(f"Error creating/updating Bedrock Guardrail: {e}")
        return False
    except Exception as e:
        print(f"Error creating/updating Bedrock Guardrail: {e}")
        return False

# ============================================================================
# Docker Build and ECR Push Functions
# ============================================================================

def check_aws_cli():
    """Check if AWS CLI is installed."""
    if not shutil.which("aws"):
        print("Error: AWS CLI is not installed")
        return False
    return True

def check_aws_credentials():
    """Check AWS credentials."""
    try:
        sts = boto3.client("sts")
        sts.get_caller_identity()
        return True
    except NoCredentialsError:
        print("Error: AWS credentials are not configured properly")
        return False
    except Exception as e:
        print(f"Error: Failed to verify AWS credentials: {e}")
        return False

DOCKER_MIN_FREE_MB = 2048
DOCKER_REQUIRED_FREE_MB = 1024


def ensure_ecr_repository(ecr_client, repository_name, region):
    """Check if ECR repository exists, create if it doesn't."""
    try:
        ecr_client.describe_repositories(repositoryNames=[repository_name])
        print(f"Repository {repository_name} exists.")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'RepositoryNotFoundException':
            print(f"Repository {repository_name} does not exist. Creating it...")
            try:
                ecr_client.create_repository(repositoryName=repository_name)
                print(f"Repository {repository_name} created successfully.")
                return True
            except Exception as create_error:
                print(f"Error: Failed to create repository: {create_error}")
                return False
        else:
            print(f"Error: Failed to check repository: {e}")
            return False


def _filesystem_free_mb(path: str) -> int:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return -1


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


def _print_disk_usage(label: str, path: str) -> int:
    free_mb = _filesystem_free_mb(path)
    if free_mb >= 0:
        print(f"  {label}: {free_mb} MB free ({path})", flush=True)
    return free_mb


def cleanup_docker_resources() -> None:
    """Remove unused Docker data to reclaim disk space before build."""
    print("===== Cleaning Up Docker Resources =====", flush=True)
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
                print(f"  ✓ Pruned {label}: {detail}", flush=True)
            else:
                err = (result.stderr or result.stdout).strip()
                print(f"  Warning: Failed to prune {label}: {err}", flush=True)
        except Exception as e:
            print(f"  Warning: Failed to prune {label}: {e}", flush=True)


def ensure_docker_disk_space(min_free_mb: int = DOCKER_MIN_FREE_MB) -> bool:
    """Ensure enough free disk space for Docker build; prune if needed."""
    print("===== Checking Docker Disk Space =====", flush=True)
    docker_root = _docker_data_root()
    root_free = _print_disk_usage("Root filesystem", "/")
    docker_free = _print_disk_usage("Docker data directory", docker_root)
    free_mb = min(root_free, docker_free) if root_free >= 0 and docker_free >= 0 else max(root_free, docker_free)

    if free_mb >= min_free_mb:
        print(f"  ✓ Sufficient disk space ({free_mb} MB >= {min_free_mb} MB)", flush=True)
        return True

    print(
        f"  Warning: Low disk space ({free_mb} MB free, need ~{min_free_mb} MB). "
        "Attempting Docker cleanup...",
        flush=True,
    )
    cleanup_docker_resources()

    root_free = _print_disk_usage("Root filesystem after cleanup", "/")
    docker_free = _print_disk_usage("Docker data directory after cleanup", docker_root)
    free_mb = min(root_free, docker_free) if root_free >= 0 and docker_free >= 0 else max(root_free, docker_free)

    if free_mb >= DOCKER_REQUIRED_FREE_MB:
        print(f"  ✓ Disk space available after cleanup ({free_mb} MB)", flush=True)
        return True

    print("Error: Not enough disk space for Docker build.", flush=True)
    print("  CloudShell and similar environments have limited storage.", flush=True)
    print("  Manual cleanup options:", flush=True)
    print("    docker system prune -af", flush=True)
    print("    rm -rf ~/.cache/pip ~/.npm /tmp/*", flush=True)
    print("  Or build elsewhere and rerun the root installer with --skip-docker-build.", flush=True)
    return False


def remove_local_docker_images(image_refs: list[str]) -> None:
    """Delete local image tags after a successful push to free disk space."""
    unique_refs = []
    for ref in image_refs:
        if ref and ref not in unique_refs:
            unique_refs.append(ref)
    if not unique_refs:
        return

    print("===== Removing Local Docker Images =====", flush=True)
    subprocess.run(
        ["docker", "rmi", "-f", *unique_refs],
        capture_output=True,
        text=True,
        check=False,
    )
    cleanup_docker_resources()


def check_docker_daemon(timeout: int = 30) -> bool:
    """Verify Docker daemon is reachable before build/push."""
    print("===== Checking Docker Daemon =====", flush=True)

    def _reachable() -> tuple[bool, str]:
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

    ok, detail = _reachable()
    if ok:
        print("  ✓ Docker daemon is running", flush=True)
        return True

    print(f"  Docker daemon not reachable: {detail}", flush=True)
    print("  Attempting to start Docker service...", flush=True)
    for cmd in (
        ["systemctl", "start", "docker"],
        ["sudo", "systemctl", "start", "docker"],
        ["service", "docker", "start"],
        ["sudo", "service", "docker", "start"],
    ):
        try:
            started = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if started.returncode == 0:
                print(f"  Started Docker via: {' '.join(cmd)}", flush=True)
                break
        except Exception:
            continue

    time.sleep(2)
    ok, detail = _reachable()
    if ok:
        print("  ✓ Docker daemon is running", flush=True)
        return True

    print(f"Error: Docker daemon is not available: {detail}")
    print(
        "  On Amazon Linux / EC2, run:\n"
        "    sudo dnf install -y docker   # or: sudo yum install -y docker\n"
        "    sudo systemctl start docker && sudo systemctl enable docker\n"
        "    sudo usermod -aG docker $USER && newgrp docker\n"
        "    docker info\n"
        "  Or build elsewhere, push to ECR, then rerun with --skip-docker-build."
    )
    return False

def docker_login(account_id, region):
    """Login to AWS ECR using Docker."""
    ecr_registry = f"{account_id}.dkr.ecr.{region}.amazonaws.com"

    try:
        print(f"  Logging in to {ecr_registry}...", flush=True)
        login_result = subprocess.run(
            ["aws", "ecr", "get-login-password", "--region", region],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if login_result.returncode != 0:
            print(f"Error: Failed to get ECR login password: {login_result.stderr.strip()}")
            return False

        docker_login_result = subprocess.run(
            ["docker", "login", "--username", "AWS", "--password-stdin", ecr_registry],
            input=login_result.stdout,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if docker_login_result.returncode != 0:
            err = docker_login_result.stderr.strip() or docker_login_result.stdout.strip()
            print(f"Error: Docker login failed: {err}")
            return False

        login_msg = docker_login_result.stdout.strip() or docker_login_result.stderr.strip()
        if login_msg:
            print(f"  {login_msg}", flush=True)
        print("  ✓ ECR login successful", flush=True)
        return True

    except subprocess.TimeoutExpired:
        print("Error: Docker login timed out. Ensure Docker Desktop is running and responsive.")
        return False
    except Exception as e:
        print(f"Error: Failed to login to ECR: {e}")
        return False

ARM64_BUILDX_BUILDER = "strands-arm64-builder"
CONTAINER_PLATFORM = "linux/arm64"


def _host_machine() -> str:
    return os.uname().machine.lower()


def _host_is_arm64() -> bool:
    return _host_machine() in ("aarch64", "arm64")


def setup_arm64_cross_build() -> bool:
    """Enable ARM64 cross-build via QEMU and buildx on x86_64 hosts."""
    print("===== Setting Up ARM64 Cross-Build =====", flush=True)
    print(f"  Host architecture: {os.uname().machine}", flush=True)
    print(f"  AgentCore requires {CONTAINER_PLATFORM} images.", flush=True)

    binfmt = subprocess.run(
        ["docker", "run", "--privileged", "--rm", "tonistiigi/binfmt", "--install", "all"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if binfmt.returncode == 0:
        print("  ✓ QEMU binfmt handlers installed", flush=True)
    else:
        err = (binfmt.stderr or binfmt.stdout).strip()
        print(f"  Warning: QEMU binfmt setup returned {binfmt.returncode}: {err}", flush=True)

    inspect = subprocess.run(
        ["docker", "buildx", "inspect", ARM64_BUILDX_BUILDER],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if inspect.returncode != 0:
        create = subprocess.run(
            [
                "docker", "buildx", "create",
                "--name", ARM64_BUILDX_BUILDER,
                "--driver", "docker-container",
                "--use",
                "--bootstrap",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if create.returncode != 0:
            err = (create.stderr or create.stdout).strip()
            print(f"Error: Failed to create buildx builder: {err}", flush=True)
            return False
    else:
        use = subprocess.run(
            ["docker", "buildx", "use", ARM64_BUILDX_BUILDER],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if use.returncode != 0:
            err = (use.stderr or use.stdout).strip()
            print(f"Error: Failed to select buildx builder: {err}", flush=True)
            return False

    platforms = subprocess.run(
        ["docker", "buildx", "inspect", "--bootstrap"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = (platforms.stdout or platforms.stderr).lower()
    if "arm64" not in output and "aarch64" not in output:
        print("Error: buildx builder does not advertise linux/arm64 support.", flush=True)
        return False

    print("  ✓ ARM64 cross-build ready (buildx + QEMU)", flush=True)
    return True


def build_and_push_arm64_image(local_tag: str, ecr_uri: str, build_args: dict[str, str] | None = None) -> bool:
    """Build an ARM64 image and push it to ECR."""
    build_context = runtime_build_context()
    dockerfile = runtime_dockerfile()

    def _with_build_args(base_command: list[str]) -> list[str]:
        command = list(base_command)
        if build_args:
            ctx_index = len(command) - 1
            for key, value in build_args.items():
                command.insert(ctx_index, f"{key}={value}")
                command.insert(ctx_index, "--build-arg")
                ctx_index += 2
        return command

    if _host_is_arm64():
        if not run_docker_command(
            _with_build_args([
                "docker", "build",
                "--platform", CONTAINER_PLATFORM,
                "--provenance=false",
                "--sbom=false",
                "-f", dockerfile,
                "-t", local_tag,
                build_context,
            ]),
            "Building Docker Image",
        ):
            return False
        if not run_docker_command(
            ["docker", "tag", local_tag, ecr_uri],
            "Tagging for ECR Repository",
        ):
            return False
        return run_docker_command(
            ["docker", "push", ecr_uri],
            "Pushing Image to ECR Repository",
        )

    if not setup_arm64_cross_build():
        return False

    return run_docker_command(
        _with_build_args([
            "docker", "buildx", "build",
            "--platform", CONTAINER_PLATFORM,
            "--provenance=false",
            "--sbom=false",
            "-f", dockerfile,
            "-t", ecr_uri,
            "--push",
            build_context,
        ]),
        "Building and Pushing Docker Image (ARM64 cross-build)",
    )


def run_docker_command(command, description):
    """Run Docker command with live output (plain BuildKit progress)."""
    print(f"===== {description} =====", flush=True)
    print(f"  $ {' '.join(command)}", flush=True)
    env = {**os.environ, "DOCKER_BUILDKIT": "1", "BUILDKIT_PROGRESS": "plain"}
    try:
        result = subprocess.run(
            command,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            print(f"Error: {description} failed (exit {result.returncode})", flush=True)
            if command and command[0] == "docker" and "build" in command:
                print(
                    "  If the build log shows 'exec format error', the host cannot run "
                    "ARM64 build steps without QEMU/buildx cross-build support.",
                    flush=True,
                )
                print(
                    "  If the build log shows 'no space left on device', run "
                    "'docker system prune -af' and retry.",
                    flush=True,
                )
            return False
        return True
    except Exception as e:
        print(f"Error: {description} failed: {e}", flush=True)
        return False

def resolve_ecr_image_tag(
    ecr_client,
    repository_name: str,
    config: dict,
    image_tag: Optional[str] = None,
) -> Optional[str]:
    """Resolve an ECR image tag from explicit input, config, or newest pushed image."""
    if image_tag:
        return image_tag

    saved_tag = config.get("latest_image_tag")
    if saved_tag:
        print(f"  Using saved image tag from config.json: {saved_tag}")
        return saved_tag

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
                print(f"  Using latest ECR image tag: {tags[0]}")
                return tags[0]
    except ClientError as e:
        print(f"Error resolving latest ECR image tag: {e}")

    return None


def verify_ecr_image_tag(ecr_client, repository_name: str, image_tag: str) -> bool:
    """Return True when the repository contains the given image tag."""
    try:
        ecr_client.describe_images(
            repositoryName=repository_name,
            imageIds=[{"imageTag": image_tag}],
        )
        return True
    except ClientError as e:
        print(f"Error: Image tag '{image_tag}' not found in {repository_name}: {e}")
        return False


def push_to_ecr(*, skip_docker_build: bool = False, image_tag: Optional[str] = None):
    """Build Docker image and push to ECR"""
    print(f"\n{'='*60}")
    print("Building Docker image and pushing to ECR")
    print(f"{'='*60}")
    
    try:
        # Use current time as tag
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        
        # Load config
        config = load_config()
        aws_account_id = config.get("accountId")
        aws_region = config.get("region")
        project_name = config.get("projectName")
        
        if not all([aws_account_id, aws_region, project_name]):
            print("Error: Missing required configuration in config.json")
            print("Required: accountId, region, projectName")
            return False
        
        # Get current folder name
        current_folder_name = os.path.basename(os.getcwd())
        print(f"CURRENT_FOLDER_NAME: {current_folder_name}")
        
        # Construct ECR repository name
        ecr_repository = f"{project_name}_{current_folder_name}"
        print(f"ECR_REPOSITORY: {ecr_repository}")
        
        # Construct image tag and ECR URI
        image_tag = timestamp
        ecr_uri = f"{aws_account_id}.dkr.ecr.{aws_region}.amazonaws.com/{ecr_repository}:{image_tag}"
        
        # Display configuration
        print("===== Checking AWS Configuration =====")
        print(f"AWS Account ID: {aws_account_id}")
        print(f"AWS Region: {aws_region}")
        print(f"ECR Repository: {ecr_repository}")
        print(f"ECR URI: {ecr_uri}")
        
        # Check AWS CLI
        if not check_aws_cli():
            return False
        
        # Check AWS credentials
        print("===== Checking AWS Credentials =====")
        if not check_aws_credentials():
            return False
        
        # Check/create ECR repository
        print("===== Checking ECR Repository =====", flush=True)
        ecr_client = boto3.client("ecr", region_name=aws_region)
        if not ensure_ecr_repository(ecr_client, ecr_repository, aws_region):
            return False

        if skip_docker_build:
            print("===== Skipping Docker build; reusing existing ECR image =====", flush=True)
            resolved_tag = resolve_ecr_image_tag(
                ecr_client, ecr_repository, config, image_tag=image_tag
            )
            if not resolved_tag:
                print(
                    "Error: No image tag found. Build and push an image from a host with Docker, "
                    "or pass --image-tag."
                )
                return False
            if not verify_ecr_image_tag(ecr_client, ecr_repository, resolved_tag):
                return False

            ecr_uri = (
                f"{aws_account_id}.dkr.ecr.{aws_region}.amazonaws.com/"
                f"{ecr_repository}:{resolved_tag}"
            )
            print(f"Using existing image: {ecr_uri}")
            update_config("latest_image_tag", resolved_tag)
            update_config("ecr_repository", ecr_repository)
            return True

        if not check_docker_daemon():
            return False

        if not ensure_docker_disk_space():
            return False

        print("===== AWS ECR Login =====", flush=True)
        if not docker_login(aws_account_id, aws_region):
            return False
        
        # Build Docker image
        print("Build output streams below (this may take several minutes)...", flush=True)
        local_tag = f"{ecr_repository}:{image_tag}"
        otel_service_name = f"{agent_runtime_name(project_name)}.DEFAULT"
        if not build_and_push_arm64_image(
            local_tag,
            ecr_uri,
            build_args={"OTEL_SERVICE_NAME": otel_service_name},
        ):
            return False
        
        # Complete
        print("===== Complete =====")
        print("Image has been successfully built and pushed to ECR.")
        print(f"Image URI: {ecr_uri}")

        remove_local_docker_images([local_tag, ecr_uri])
        
        # Store image tag in config for later use
        update_config('latest_image_tag', image_tag)
        update_config('ecr_repository', ecr_repository)
        
        return True
        
    except Exception as e:
        print(f"Error building and pushing Docker image: {e}")
        return False

# ============================================================================
# Agent Runtime Creation/Update Functions
# ============================================================================

def get_latest_image_tag(config):
    """Return the image tag to deploy for AgentCore.

    Prefer ``latest_image_tag`` written by the build that just finished. Falling
    back to an unpaginated ECR ``describe_images`` sort can pick a stale tag
    (seen: built 20260805172059 but runtime stayed on 20260805165556).
    """
    configured = (config.get("latest_image_tag") or "").strip()
    if configured:
        print(f"Using configured image tag: {configured}")
        return configured

    try:
        aws_region = config['region']
        project_name = config.get('projectName')
        current_folder_name = os.path.basename(os.getcwd())
        repository_name = f"{project_name}_{current_folder_name}"

        ecr_client = boto3.client('ecr', region_name=aws_region)
        images: list = []
        token = None
        while True:
            kwargs = {"repositoryName": repository_name, "maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            response = ecr_client.describe_images(**kwargs)
            images.extend(response.get("imageDetails") or [])
            token = response.get("nextToken")
            if not token:
                break

        tagged = [img for img in images if img.get("imageTags")]
        if not tagged:
            print(f"Error: No tagged images found in repository {repository_name}")
            return None

        images_sorted = sorted(tagged, key=lambda x: x["imagePushedAt"], reverse=True)
        image_tag = images_sorted[0]["imageTags"][0]
        print(f"Latest image tag from ECR: {image_tag}")
        return image_tag

    except Exception as e:
        print(f"Error getting latest image tag: {e}")
        return None

def _load_application_config_file() -> dict:
    """Load application/config.json, returning {} when missing or invalid."""
    app_config_path = os.path.join(_repo_root(), "application", "config.json")
    try:
        with open(app_config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def sync_ecs_app_config(app_config: dict) -> bool:
    """Push application config into the running ECS task as APP_CONFIG_JSON.

    Runtime installer updates local config files, but ECS Streamlit containers read
    APP_CONFIG_JSON from the task definition (via docker-entrypoint.sh). Without this
    sync, ECS keeps a stale or missing agent_runtime_arn after runtime create/update.
    """
    print(f"\n{'='*60}")
    print("Syncing ECS APP_CONFIG_JSON with agent_runtime_arn")
    print(f"{'='*60}")

    project_name = app_config.get("projectName") or ""
    aws_region = app_config.get("region") or ""
    agent_runtime_arn = app_config.get("agent_runtime_arn") or ""

    if not project_name or not aws_region:
        print("Warning: projectName/region missing; skipping ECS config sync")
        return True
    if not agent_runtime_arn:
        print("Warning: agent_runtime_arn missing; skipping ECS config sync")
        return True

    cluster_name = f"cluster-for-{project_name}"
    service_name = f"service-for-{project_name}"
    container_name = "app"

    try:
        ecs = boto3.client("ecs", region_name=aws_region)
        services = ecs.describe_services(cluster=cluster_name, services=[service_name])
        service_list = services.get("services") or []
        if not service_list or service_list[0].get("status") == "INACTIVE":
            print(
                f"  ECS service not found ({cluster_name}/{service_name}); "
                "skipping sync (UI may not be deployed yet)"
            )
            return True

        service = service_list[0]
        task_definition_arn = service.get("taskDefinition")
        if not task_definition_arn:
            print("Warning: ECS service has no task definition; skipping sync")
            return True

        task_def = ecs.describe_task_definition(taskDefinition=task_definition_arn)
        td = task_def["taskDefinition"]
        containers = td.get("containerDefinitions") or []
        if not containers:
            print("Warning: task definition has no containers; skipping sync")
            return True

        target = None
        for container in containers:
            if container.get("name") == container_name:
                target = container
                break
        if target is None:
            target = containers[0]
            print(
                f"  Warning: container '{container_name}' not found; "
                f"updating '{target.get('name')}' instead"
            )

        env_vars = list(target.get("environment") or [])
        config_json = json.dumps(app_config, ensure_ascii=False)
        updated = False
        for item in env_vars:
            if item.get("name") == "APP_CONFIG_JSON":
                item["value"] = config_json
                updated = True
                break
        if not updated:
            env_vars.append({"name": "APP_CONFIG_JSON", "value": config_json})
        target["environment"] = env_vars

        register_kwargs = {
            "family": td["family"],
            "containerDefinitions": containers,
            "requiresCompatibilities": td.get("requiresCompatibilities") or ["FARGATE"],
            "networkMode": td.get("networkMode") or "awsvpc",
            "cpu": td.get("cpu"),
            "memory": td.get("memory"),
        }
        for optional_key in (
            "executionRoleArn",
            "taskRoleArn",
            "volumes",
            "placementConstraints",
            "runtimePlatform",
            "ephemeralStorage",
            "proxyConfiguration",
            "ipcMode",
            "pidMode",
        ):
            if td.get(optional_key) is not None:
                register_kwargs[optional_key] = td[optional_key]

        response = ecs.register_task_definition(**register_kwargs)
        new_task_def_arn = response["taskDefinition"]["taskDefinitionArn"]
        print(f"  ✓ Registered task definition: {new_task_def_arn}")

        ecs.update_service(
            cluster=cluster_name,
            service=service_name,
            taskDefinition=new_task_def_arn,
            forceNewDeployment=True,
        )
        print(
            f"  ✓ Updated ECS service '{service_name}' with agent_runtime_arn "
            f"and forced new deployment"
        )
        print(f"  agent_runtime_arn: {agent_runtime_arn}")
        return True
    except ClientError as e:
        print(f"Warning: Failed to sync ECS APP_CONFIG_JSON: {e}")
        print("  Local config was updated; redeploy ECS or rerun root installer to apply.")
        return True
    except Exception as e:
        print(f"Warning: Failed to sync ECS APP_CONFIG_JSON: {e}")
        return True


def update_agentcore_json(agent_runtime_arn, image_tag=None):
    """Update config.json with agent runtime ARN and sync application + ECS config."""
    try:
        update_config('agent_runtime_arn', agent_runtime_arn)
        if image_tag:
            update_config('latest_image_tag', image_tag)
        print(f"✓ config.json updated with agent_runtime_arn: {agent_runtime_arn}")

        app_config_path = os.path.join(_repo_root(), "application", "config.json")
        app_config = _load_application_config_file()
        if os.path.isfile(app_config_path):
            app_config["agent_runtime_arn"] = agent_runtime_arn
            if image_tag:
                app_config["agent_latest_image_tag"] = image_tag
            with open(app_config_path, "w", encoding="utf-8") as f:
                json.dump(app_config, f, ensure_ascii=False, indent=2)
            print(f"✓ application/config.json synced (agent_latest_image_tag={image_tag})")

            sync_ecs_app_config(app_config)
        else:
            print("Warning: application/config.json not found; skipping ECS sync")
        return True
    except Exception as e:
        print(f"Error updating config.json: {e}")
        return False

SESSION_STORAGE_MOUNT_PATH = "/mnt/workspace"


def _uses_s3_files_session_storage(config: dict) -> bool:
    return bool(config.get("s3_files_access_point_arn"))


def session_storage_filesystem_configurations(config: dict):
    """Filesystem mount for AgentCore session persistence."""
    access_point_arn = config.get("s3_files_access_point_arn")
    if access_point_arn:
        return [
            {
                "s3FilesAccessPoint": {
                    "accessPointArn": access_point_arn,
                    "mountPath": SESSION_STORAGE_MOUNT_PATH,
                }
            }
        ]

    return [
        {
            "sessionStorage": {
                "mountPath": SESSION_STORAGE_MOUNT_PATH,
            }
        }
    ]


def agent_runtime_network_configuration(config: dict):
    """Use VPC mode when S3 Files access point is configured."""
    if not _uses_s3_files_session_storage(config):
        return {"networkMode": "PUBLIC"}

    subnets = config.get("agent_runtime_vpc_subnets") or []
    security_groups = config.get("agent_runtime_security_groups") or []
    if not subnets or not security_groups:
        print(
            "Error: s3_files_access_point_arn is set but agent_runtime_vpc_subnets "
            "or agent_runtime_security_groups is missing."
        )
        return None

    return {
        "networkMode": "VPC",
        "networkModeConfig": {
            "subnets": subnets,
            "securityGroups": security_groups,
        },
    }


def verify_session_storage_config(client, agent_runtime_id: str, config: dict) -> bool:
    """Confirm session storage is configured on the agent runtime."""
    response = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)
    filesystem_configs = response.get("filesystemConfigurations") or []
    for cfg in filesystem_configs:
        if _uses_s3_files_session_storage(config):
            s3_files = cfg.get("s3FilesAccessPoint") or {}
            mount_path = s3_files.get("mountPath")
            if mount_path == SESSION_STORAGE_MOUNT_PATH:
                print(
                    "✓ s3FilesAccessPoint verified: "
                    f"mountPath={mount_path}, arn={s3_files.get('accessPointArn')}"
                )
                return True
        else:
            session_storage = cfg.get("sessionStorage") or {}
            mount_path = session_storage.get("mountPath")
            if mount_path == SESSION_STORAGE_MOUNT_PATH:
                print(f"✓ sessionStorage verified: mountPath={mount_path}")
                return True

    storage_type = "s3FilesAccessPoint" if _uses_s3_files_session_storage(config) else "sessionStorage"
    print(
        f"⚠ {storage_type} not found in filesystemConfigurations. "
        f"Cold start may not restore {SESSION_STORAGE_MOUNT_PATH} session data."
    )
    print(f"  filesystemConfigurations: {filesystem_configs or None}")
    return False


def create_agent_runtime_func(config, repository_name, image_tag):
    """Create a new Agent Runtime."""
    aws_region = config['region']
    account_id = config['accountId']
    agent_runtime_role = config.get('agent_runtime_role')
    
    if not agent_runtime_role:
        print("Error: agent_runtime_role not found in config.json")
        return None
    
    project_name = config.get("projectName")
    if not project_name:
        print("Error: projectName not found in config.json")
        return None

    runtime_name = agent_runtime_name(project_name)
    print(f"Creating agent runtime: {runtime_name}")
    
    try:
        client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
        network_configuration = agent_runtime_network_configuration(config)
        if network_configuration is None:
            return None
        
        response = client.create_agent_runtime(
            agentRuntimeName=runtime_name,
            agentRuntimeArtifact={
                'containerConfiguration': {
                    'containerUri': f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/{repository_name}:{image_tag}"
                }
            },
            filesystemConfigurations=session_storage_filesystem_configurations(config),
            networkConfiguration=network_configuration,
            roleArn=agent_runtime_role
        )
        
        print(f"✓ Agent runtime created: {response['agentRuntimeArn']}")
        return response['agentRuntimeArn']
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConflictException':
            print(f"Agent runtime {runtime_name} already exists")
            return None
        else:
            print(f"Error creating agent runtime: {e}")
            return None
    except Exception as e:
        print(f"Error creating agent runtime: {e}")
        return None

def update_agent_runtime_func(config, repository_name, agent_runtime_id, image_tag):
    """Update an existing Agent Runtime."""
    aws_region = config['region']
    account_id = config['accountId']
    agent_runtime_role = config.get('agent_runtime_role')
    
    if not agent_runtime_role:
        print("Error: agent_runtime_role not found in config.json")
        return None
    
    print(f"Updating agent runtime: {repository_name}")
    
    try:
        client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
        network_configuration = agent_runtime_network_configuration(config)
        if network_configuration is None:
            return None
        
        response = client.update_agent_runtime(
            agentRuntimeId=agent_runtime_id,
            description="Update agent runtime",
            agentRuntimeArtifact={
                'containerConfiguration': {
                    'containerUri': f"{account_id}.dkr.ecr.{aws_region}.amazonaws.com/{repository_name}:{image_tag}"
                }
            },
            filesystemConfigurations=session_storage_filesystem_configurations(config),
            roleArn=agent_runtime_role,
            networkConfiguration=network_configuration,
            protocolConfiguration={"serverProtocol": "HTTP"}
        )
        
        print(f"✓ Agent runtime updated: {response['agentRuntimeArn']}")
        return response['agentRuntimeArn']
        
    except Exception as e:
        print(f"Error updating agent runtime: {e}")
        return None

def create_agent_runtime():
    """Create/update AgentCore runtime"""
    print(f"\n{'='*60}")
    print("Creating/updating AgentCore runtime")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        aws_region = config['region']
        project_name = config.get('projectName')
        
        # Get current folder name
        current_folder_name = os.path.basename(os.getcwd())
        repository_name = f"{project_name}_{current_folder_name}"
        runtime_name = agent_runtime_name(project_name)
        
        print(f"Repository name: {repository_name}")
        print(f"Runtime name: {runtime_name}")
        
        # Get latest image tag
        image_tag = get_latest_image_tag(config)
        if not image_tag:
            print("Error: Could not get latest image tag")
            return False
        
        print(f"Using image tag: {image_tag}")
        if _uses_s3_files_session_storage(config):
            print("Session storage: S3 Files access point at /mnt/workspace (VPC mode)")
        else:
            print("Session storage: managed sessionStorage at /mnt/workspace (PUBLIC mode)")
        
        # Check if agent runtime already exists
        client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
        response = client.list_agent_runtimes()
        agent_runtimes = response.get('agentRuntimes', [])
        
        is_exist = False
        agent_runtime_id = None
        
        for agent_runtime in agent_runtimes:
            if agent_runtime['agentRuntimeName'] == runtime_name:
                print(f"Agent runtime {runtime_name} already exists")
                is_exist = True
                agent_runtime_id = agent_runtime['agentRuntimeId']
                break
        
        # Create or update agent runtime
        if is_exist:
            print(f"Updating agent runtime: {runtime_name}")
            agent_runtime_arn = update_agent_runtime_func(config, repository_name, agent_runtime_id, image_tag)
        else:
            print(f"Creating agent runtime: {runtime_name}")
            agent_runtime_arn = create_agent_runtime_func(config, repository_name, image_tag)
        
        if not agent_runtime_arn:
            print("Error: Failed to create/update agent runtime")
            return False

        resolved_runtime_id = agent_runtime_id
        if not resolved_runtime_id and agent_runtime_arn:
            resolved_runtime_id = agent_runtime_arn.rsplit("/", 1)[-1]

        if resolved_runtime_id:
            verify_session_storage_config(client, resolved_runtime_id, config)
        
        # Update config.json
        update_agentcore_json(agent_runtime_arn, image_tag)
        
        print("\n✓ Agent runtime creation/update completed")
        return True
        
    except Exception as e:
        print(f"Error creating/updating agent runtime: {e}")
        return False


def setup_agentcore_observability():
    """Enable Transaction Search and trace delivery for AgentCore Observability."""
    print(f"\n{'='*60}")
    print("Configuring AgentCore Observability")
    print(f"{'='*60}")

    try:
        from observability import setup_agentcore_observability as configure_observability

        config = load_config()
        region = config.get("region")
        account_id = config.get("accountId")
        runtime_arn = config.get("agent_runtime_arn")

        if not region or not account_id:
            print("Warning: region or accountId missing in config.json; skipping observability setup")
            return True

        result = configure_observability(runtime_arn, region, account_id)
        warning = result.get("warning")
        if warning:
            print(f"Warning: {warning}")
        else:
            print("✓ AgentCore Observability configured")
        return True
    except Exception as e:
        print(f"Warning: AgentCore Observability setup failed: {e}")
        return True


def setup_agentcore_evaluations():
    """Create evaluation IAM role and online evaluation configuration."""
    print(f"\n{'='*60}")
    print("Configuring AgentCore Evaluations")
    print(f"{'='*60}")

    try:
        from evaluation import setup_agentcore_evaluation

        config = load_config()
        region = config.get("region")
        account_id = config.get("accountId")
        runtime_arn = config.get("agent_runtime_arn")
        project_name = config.get("projectName", "strands-runtime")

        if not region or not account_id:
            print("Warning: region or accountId missing in config.json; skipping evaluation setup")
            return True

        result = setup_agentcore_evaluation(
            runtime_arn,
            region,
            account_id,
            project_name,
        )
        warning = result.get("warning")
        role_arn = result.get("evaluation_execution_role_arn")
        config_name = result.get("online_evaluation_config_name")

        if role_arn:
            update_config("evaluation_execution_role_arn", role_arn)
        if config_name:
            update_config("online_evaluation_config_name", config_name)
        config_id = result.get("online_evaluation_config_id")
        if config_id:
            update_config("online_evaluation_config_id", config_id)
        if result.get("service_name"):
            update_config("evaluation_service_name", result["service_name"])
        if result.get("log_group"):
            update_config("evaluation_log_group", result["log_group"])
        if result.get("session_timeout_minutes") is not None:
            update_config(
                "evaluation_session_timeout_minutes",
                result["session_timeout_minutes"],
            )

        if warning:
            print(f"Warning: {warning}")
        elif result.get("status") != "skipped":
            print("✓ AgentCore Evaluations configured")
        return True
    except Exception as e:
        print(f"Warning: AgentCore Evaluations setup failed: {e}")
        return True


def create_monitoring_dashboard():
    """Create or update CloudWatch dashboard for Strands AgentCore runtime."""
    print(f"\n{'='*60}")
    print("Creating CloudWatch monitoring dashboard")
    print(f"{'='*60}")

    try:
        from cloudwatch_metrics import create_bedrock_usage_dashboard, create_cloudwatch_dashboard

        config = load_config()
        project_name = config.get("projectName", "strands-runtime")
        region = config.get("region")
        agent_runtime_arn = config.get("agent_runtime_arn")

        if not region:
            print("Error: region not found in config.json")
            return False

        bedrock_dashboard = create_bedrock_usage_dashboard(region)
        if bedrock_dashboard:
            update_config("bedrock_usage_dashboard_name", bedrock_dashboard)

        dashboard = create_cloudwatch_dashboard(project_name, agent_runtime_arn, region)
        if dashboard:
            update_config("cloudwatch_dashboard_name", dashboard)
            return True

        if bedrock_dashboard:
            return True

        print("Warning: CloudWatch dashboard was not created")
        return True

    except Exception as e:
        print(f"Error creating CloudWatch dashboard: {e}")
        return False

# ============================================================================
# Main Function
# ============================================================================

def main():
    """Main function: Execute the entire installation process."""
    parser = argparse.ArgumentParser(description="AgentCore Runtime Installation Script")
    parser.add_argument(
        "--skip-docker-build",
        action="store_true",
        help="Skip local Docker build/push and reuse an existing image in ECR.",
    )
    parser.add_argument(
        "--image-tag",
        metavar="TAG",
        help="ECR image tag to use with --skip-docker-build (default: config or latest in ECR).",
    )
    args = parser.parse_args()

    if args.image_tag and not args.skip_docker_build:
        print("Warning: --image-tag is only used with --skip-docker-build")

    print("\n" + "="*60)
    print("AgentCore Runtime Installation Script")
    print("="*60)
    
    # Check config.json
    config = load_config()
    
    print(f"Configuration file loaded successfully")
    print(f"  - Project Name: {config.get('projectName')}")
    print(f"  - Region: {config.get('region')}")
    print(f"  - Account ID: {config.get('accountId')}")

    def push_step() -> bool:
        return push_to_ecr(
            skip_docker_build=args.skip_docker_build,
            image_tag=args.image_tag,
        )

    docker_step_name = (
        "Reusing existing ECR image"
        if args.skip_docker_build
        else "Building Docker image and pushing to ECR"
    )
    
    # Execute each step
    steps = [
        ("Updating Knowledge Base configuration", update_knowledge_base_config),
        ("Creating IAM policies and roles", create_iam_policies),
        ("Creating/updating Amazon Bedrock Guardrail", create_bedrock_guardrail),
        (docker_step_name, push_step),
        ("Creating/updating AgentCore runtime", create_agent_runtime),
        ("Configuring AgentCore Observability", setup_agentcore_observability),
        ("Configuring AgentCore Evaluations", setup_agentcore_evaluations),
        ("Creating CloudWatch monitoring dashboard", create_monitoring_dashboard),
    ]
    
    for step_name, step_func in steps:
        if not step_func():
            print(f"\nInstallation failed: Error occurred in step '{step_name}'.")
            print("   Previous steps completed, but installation was aborted.")
            sys.exit(1)
    
    # Output final results
    print("\n" + "="*60)
    print("All installation steps completed successfully!")
    print("="*60)
    
    # Output final config.json information
    config = load_config()
    
    role_arn = config.get('agent_runtime_role')
    arn = config.get('agent_runtime_arn')
    knowledge_base_name = config.get('knowledge_base_name')
    knowledge_base_id = config.get('knowledge_base_id')
    
    if knowledge_base_name:
        print(f"\nKnowledge Base Name: {knowledge_base_name}")
    if knowledge_base_id:
        print(f"Knowledge Base ID: {knowledge_base_id}")
    guardrail_id = config.get("guardrail_id")
    guardrail_name_value = config.get("guardrail_name")
    if guardrail_name_value:
        print(f"Bedrock Guardrail Name: {guardrail_name_value}")
    if guardrail_id:
        print(f"Bedrock Guardrail ID: {guardrail_id}")
        print(f"Bedrock Guardrail Version: {config.get('guardrail_version', 'DRAFT')}")
    if role_arn:
        print(f"Created AgentCore Runtime Role ARN: {role_arn}")
    if arn:
        print(f"Created AgentCore Runtime ARN: {arn}")

    dashboard_name = config.get("cloudwatch_dashboard_name")
    bedrock_dashboard_name = config.get("bedrock_usage_dashboard_name")
    region = config.get("region", "us-west-2")
    if bedrock_dashboard_name:
        print(f"Bedrock Usage Dashboard: {bedrock_dashboard_name}")
        print(
            f"  https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{bedrock_dashboard_name}"
        )
    if dashboard_name:
        print(f"CloudWatch Dashboard: {dashboard_name}")
        print(
            f"  https://{region}.console.aws.amazon.com/cloudwatch/home"
            f"?region={region}#dashboards/dashboard/{dashboard_name}"
        )
    
    if role_arn and arn:
        print("\nInstallation complete!")
    else:
        print("\nInstallation completed with warnings!")

if __name__ == "__main__":
    main()
