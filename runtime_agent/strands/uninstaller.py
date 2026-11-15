#!/usr/bin/env python3
"""
Unified uninstallation script
Sequentially deletes:
  Online evaluation -> Guardrail -> CloudWatch dashboards ->
  AgentCore runtime -> ECR repository -> IAM role -> IAM policy
All functionality integrated into a single file
"""

import sys
import os
import json
import time
import argparse
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")


def _repo_root() -> str:
    return os.path.normpath(os.path.join(script_dir, "..", ".."))


def _load_application_config() -> dict:
    app_config_path = os.path.join(_repo_root(), "application", "config.json")
    try:
        with open(app_config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _get_root_installer_value(prefix: str) -> str:
    root_installer_path = os.path.join(_repo_root(), "installer.py")
    try:
        with open(root_installer_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{prefix} = "):
                    value = stripped.split("=", 1)[1].strip()
                    if "#" in value:
                        value = value.split("#", 1)[0].strip()
                    return value.strip('"').strip("'")
    except OSError:
        pass
    return ""


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

    region = _load_application_config().get("region") or ""
    if region:
        return region

    region = _get_root_installer_value("region")
    if region:
        return region

    return os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-west-2"


def _create_config_from_session() -> dict:
    """Initialize config fields from boto3 session when config.json is missing."""
    region = _resolve_region({})
    project_name = (
        _load_application_config().get("projectName")
        or _get_root_installer_value("project_name")
        or "strands-runtime"
    )
    config = {
        "region": region,
        "projectName": project_name,
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


def _ensure_config_defaults(config: dict) -> dict:
    updated = dict(config)
    changed = False

    region = _resolve_region(updated)
    if updated.get("region") != region:
        updated["region"] = region
        changed = True

    account_id = updated.get("accountId") or _load_application_config().get("accountId")
    if not account_id:
        try:
            account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        except Exception:
            account_id = ""
    if account_id and updated.get("accountId") != account_id:
        updated["accountId"] = account_id
        changed = True

    project_name = updated.get("projectName") or _load_application_config().get("projectName") or _get_root_installer_value("project_name")
    if not project_name:
        project_name = "strands-runtime"
    if updated.get("projectName") != project_name:
        updated["projectName"] = project_name
        changed = True

    if changed:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(updated, f, indent=2)

    return updated


def load_config():
    """Load config.json file."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        print("config.json not found, initializing from boto3 session...")
        config = _create_config_from_session()
    except Exception as e:
        print(f"Failed to parse config.json file: {e}")
        print("Error: config.json file is required for uninstallation")
        return None

    return _ensure_config_defaults(config)

# ============================================================================
# Agent Runtime Deletion Functions
# ============================================================================

def agent_runtime_name(project_name: str) -> str:
    """Return Bedrock AgentCore runtime name (e.g. strands_runtime)."""
    return project_name.replace("-", "_")


def delete_agent_runtime():
    """Delete AgentCore runtime and wait for deletion to complete"""
    print(f"\n{'='*60}")
    print("Deleting AgentCore runtime")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        if not config:
            return False
            
        aws_region = config.get('region')
        project_name = config.get('projectName')
        agent_runtime_arn = config.get('agent_runtime_arn')
        
        if not all([aws_region, project_name]):
            print("Error: Missing required configuration in config.json")
            print("Required: region, projectName")
            return False
        
        # Get current folder name
        current_folder_name = os.path.basename(os.getcwd())
        repository_name = f"{project_name}_{current_folder_name}"
        runtime_name = agent_runtime_name(project_name)
        legacy_runtime_name = agent_runtime_name(current_folder_name)
        legacy_prefixed_project = f"runtime_{project_name.replace('-', '_')}"
        legacy_prefixed_type = f"runtime_{current_folder_name.replace('-', '_')}"
        candidate_runtime_names = [runtime_name]
        for legacy_name in (
            legacy_runtime_name,
            legacy_prefixed_project,
            legacy_prefixed_type,
        ):
            if legacy_name not in candidate_runtime_names:
                candidate_runtime_names.append(legacy_name)
        legacy_repository_name = repository_name.replace("-", "_")
        if legacy_repository_name not in candidate_runtime_names:
            candidate_runtime_names.append(legacy_repository_name)
        
        try:
            client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
            deletion_requested = False
            actual_runtime_name = None
            
            # If agent_runtime_arn is in config, use it
            if agent_runtime_arn:
                # Extract agent runtime ID from ARN
                # ARN format: arn:aws:bedrock-agentcore:region:account:runtime/runtime-name-runtimeId
                runtime_id = agent_runtime_arn.split('/')[-1] if '/' in agent_runtime_arn else None
                
                if runtime_id:
                    try:
                        client.delete_agent_runtime(agentRuntimeId=runtime_id)
                        print(f"✓ Agent runtime deletion requested: {agent_runtime_arn}")
                        deletion_requested = True
                        # Get actual runtime name from ARN or list
                        try:
                            response = client.list_agent_runtimes()
                            agent_runtimes = response.get('agentRuntimes', [])
                            for agent_runtime in agent_runtimes:
                                if agent_runtime['agentRuntimeId'] == runtime_id:
                                    actual_runtime_name = agent_runtime['agentRuntimeName']
                                    break
                        except:
                            actual_runtime_name = runtime_name
                    except ClientError as e:
                        if e.response['Error']['Code'] == 'ResourceNotFoundException':
                            print(f"Agent runtime not found (may already be deleted): {agent_runtime_arn}")
                            return True
                        else:
                            print(f"Error deleting agent runtime: {e}")
                            return False
            
            # Fallback: List and find by name
            if not deletion_requested:
                response = client.list_agent_runtimes()
                agent_runtimes = response.get('agentRuntimes', [])
                
                for agent_runtime in agent_runtimes:
                    if agent_runtime['agentRuntimeName'] in candidate_runtime_names:
                        runtime_id = agent_runtime['agentRuntimeId']
                        actual_runtime_name = agent_runtime['agentRuntimeName']
                        try:
                            client.delete_agent_runtime(agentRuntimeId=runtime_id)
                            print(f"✓ Agent runtime deletion requested: {agent_runtime['agentRuntimeArn']}")
                            deletion_requested = True
                            break
                        except ClientError as e:
                            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                                print(f"Agent runtime not found (may already be deleted): {actual_runtime_name}")
                                return True
                            else:
                                print(f"Error deleting agent runtime: {e}")
                                return False
                
                if not deletion_requested:
                    print(
                        f"Agent runtime {candidate_runtime_names} not found "
                        "(may already be deleted)"
                    )
                    return True
            
            # Wait for deletion to complete
            if deletion_requested:
                # Use actual runtime name if available, otherwise use runtime_name
                name_to_check = actual_runtime_name if actual_runtime_name else runtime_name
                return wait_for_runtime_deletion(config, name_to_check)
            else:
                return True
            
        except Exception as e:
            print(f"Error deleting agent runtime: {e}")
            return False
            
    except Exception as e:
        print(f"Error deleting agent runtime: {e}")
        return False

def wait_for_runtime_deletion(config, runtime_name, max_wait_time=600):
    """Wait for AgentCore runtime to be completely deleted (check every 10 seconds)"""
    aws_region = config.get('region')
    if not aws_region:
        print("Error: region not found in config.json")
        return False
    
    print(f"\nWaiting for AgentCore runtime '{runtime_name}' to be deleted...")
    print("Checking every 10 seconds...")
    
    client = boto3.client('bedrock-agentcore-control', region_name=aws_region)
    start_time = time.time()
    check_count = 0
    
    while True:
        check_count += 1
        elapsed_time = time.time() - start_time
        
        try:
            response = client.list_agent_runtimes()
            agent_runtimes = response.get('agentRuntimes', [])
            
            # Check if the specific runtime still exists
            runtime_exists = False
            for agent_runtime in agent_runtimes:
                if agent_runtime['agentRuntimeName'] == runtime_name:
                    runtime_exists = True
                    break
            
            if not runtime_exists:
                print(f"✓ AgentCore runtime '{runtime_name}' has been successfully deleted")
                print(f"  (Checked {check_count} times, elapsed time: {elapsed_time:.1f} seconds)")
                return True
            
            # Check timeout
            if elapsed_time >= max_wait_time:
                print(f"\nTimeout: AgentCore runtime '{runtime_name}' still exists after {max_wait_time} seconds")
                print("  Please check manually or try again later")
                return False
            
            # Wait 10 seconds before next check
            print(f"  [{check_count}] Runtime still exists, waiting 10 seconds... (elapsed: {elapsed_time:.1f}s)")
            time.sleep(10)
            
        except Exception as e:
            print(f"Error checking runtime status: {e}")
            return False

# ============================================================================
# ECR Repository Deletion Functions
# ============================================================================

def delete_ecr_repository():
    """Delete ECR repository and all images"""
    print(f"\n{'='*60}")
    print("Deleting ECR repository")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        if not config:
            return False
            
        aws_region = config.get('region')
        project_name = config.get('projectName')
        ecr_repository = config.get('ecr_repository')
        
        if not all([aws_region, project_name]):
            print("Error: Missing required configuration in config.json")
            print("Required: region, projectName")
            return False
        
        # Get repository name
        if not ecr_repository:
            # Get current folder name
            current_folder_name = os.path.basename(os.getcwd())
            ecr_repository = f"{project_name}_{current_folder_name}"
        
        print(f"Repository name: {ecr_repository}")
        
        try:
            ecr_client = boto3.client('ecr', region_name=aws_region)
            
            # Check if repository exists
            try:
                ecr_client.describe_repositories(repositoryNames=[ecr_repository])
            except ClientError as e:
                if e.response['Error']['Code'] == 'RepositoryNotFoundException':
                    print(f"ECR repository {ecr_repository} not found (may already be deleted)")
                    return True
                else:
                    print(f"Error checking repository: {e}")
                    return False
            
            # List all images in the repository
            try:
                response = ecr_client.list_images(repositoryName=ecr_repository)
                image_ids = response.get('imageIds', [])
                
                if image_ids:
                    print(f"Found {len(image_ids)} images in repository. Deleting...")
                    # Delete all images
                    ecr_client.batch_delete_image(
                        repositoryName=ecr_repository,
                        imageIds=image_ids
                    )
                    print(f"✓ Deleted {len(image_ids)} images from repository")
                else:
                    print("No images found in repository")
            except ClientError as e:
                if e.response['Error']['Code'] != 'RepositoryNotFoundException':
                    print(f"Warning: Error deleting images: {e}")
            
            # Delete repository
            try:
                ecr_client.delete_repository(
                    repositoryName=ecr_repository,
                    force=True  # Force delete even if images exist
                )
                print(f"✓ ECR repository deleted: {ecr_repository}")
                return True
            except ClientError as e:
                if e.response['Error']['Code'] == 'RepositoryNotFoundException':
                    print(f"ECR repository {ecr_repository} not found (may already be deleted)")
                    return True
                else:
                    print(f"Error deleting repository: {e}")
                    return False
                    
        except Exception as e:
            print(f"Error deleting ECR repository: {e}")
            return False
            
    except Exception as e:
        print(f"Error deleting ECR repository: {e}")
        return False

# ============================================================================
# IAM Role and Policy Deletion Functions
# ============================================================================

def detach_policy_from_role(role_name, policy_arn):
    """Detach policy from IAM role"""
    try:
        iam_client = boto3.client('iam')
        
        # Detach policy from role
        iam_client.detach_role_policy(
            RoleName=role_name,
            PolicyArn=policy_arn
        )
        print(f"✓ Policy detached successfully: {policy_arn}")
        return True
        
    except ClientError as e:
        if e.response['Error']['Code'] == 'NoSuchEntity':
            print(f"Policy not attached to role (may already be detached): {policy_arn}")
            return True
        else:
            print(f"Policy detachment failed: {e}")
            return False
    except Exception as e:
        print(f"Policy detachment failed: {e}")
        return False

def delete_iam_role(config):
    """Delete IAM role"""
    projectName = config.get('projectName', 'agentcore')
    role_name = f"AmazonBedrockAgentCoreRuntimeRoleFor{projectName}"
    
    try:
        iam_client = boto3.client('iam')
        
        # Check if role exists
        try:
            existing_role = iam_client.get_role(RoleName=role_name)
            role_arn = existing_role['Role']['Arn']
            
            # List attached policies
            attached_policies = iam_client.list_attached_role_policies(RoleName=role_name)
            for policy in attached_policies.get('AttachedPolicies', []):
                detach_policy_from_role(role_name, policy['PolicyArn'])
            
            # List inline policies
            inline_policies = iam_client.list_role_policies(RoleName=role_name)
            for policy_name in inline_policies.get('PolicyNames', []):
                try:
                    iam_client.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
                    print(f"✓ Deleted inline policy: {policy_name}")
                except Exception as e:
                    print(f"Warning: Failed to delete inline policy {policy_name}: {e}")
            
            # Delete role
            iam_client.delete_role(RoleName=role_name)
            print(f"✓ IAM role deleted: {role_arn}")
            return True
            
        except iam_client.exceptions.NoSuchEntityException:
            print(f"IAM role {role_name} not found (may already be deleted)")
            return True
            
    except Exception as e:
        print(f"Role deletion failed: {e}")
        return False

def _delete_one_iam_policy(iam_client, account_id: str, policy_name: str) -> bool:
    """Delete one customer managed policy and all of its versions."""
    policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
    try:
        try:
            iam_client.get_policy(PolicyArn=policy_arn)
        except iam_client.exceptions.NoSuchEntityException:
            print(f"IAM policy {policy_name} not found (may already be deleted)")
            return True

        versions_response = iam_client.list_policy_versions(PolicyArn=policy_arn)
        versions = versions_response["Versions"]

        for version in versions:
            if not version["IsDefaultVersion"]:
                try:
                    iam_client.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=version["VersionId"],
                    )
                    print(f"✓ Deleted policy version: {version['VersionId']}")
                except Exception as e:
                    print(
                        f"Warning: Failed to delete policy version "
                        f"{version['VersionId']}: {e}"
                    )

        if versions:
            default_version = next(
                (v for v in versions if v["IsDefaultVersion"]), None
            )
            if default_version:
                try:
                    iam_client.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=default_version["VersionId"],
                    )
                    print(
                        f"✓ Deleted default policy version: "
                        f"{default_version['VersionId']}"
                    )
                except Exception as e:
                    print(f"Warning: Failed to delete default policy version: {e}")

        iam_client.delete_policy(PolicyArn=policy_arn)
        print(f"✓ IAM policy deleted: {policy_arn}")
        return True
    except Exception as e:
        print(f"Policy deletion failed for {policy_name}: {e}")
        return False


def delete_iam_policy(config):
    """Delete IAM policies (core + storage split) and all versions."""
    accountId = config.get('accountId')
    projectName = config.get('projectName', 'agentcore')
    policy_names = [
        f"AmazonBedrockAgentCoreRuntimePolicyFor{projectName}",
        f"AmazonBedrockAgentCoreRuntimeStorageFor{projectName}",
    ]

    try:
        iam_client = boto3.client('iam')
        ok = True
        for policy_name in policy_names:
            if not _delete_one_iam_policy(iam_client, accountId, policy_name):
                ok = False
        return ok
    except Exception as e:
        print(f"Policy deletion failed: {e}")
        return False

def delete_iam_resources():
    """Delete IAM role and policy"""
    print(f"\n{'='*60}")
    print("Deleting IAM role and policy")
    print(f"{'='*60}")
    
    try:
        config = load_config()
        if not config:
            return False
        
        accountId = config.get('accountId')
        if not accountId:
            print("Error: accountId not found in config.json")
            return False
        
        # Delete role first (it references the policy)
        print("\n1. Deleting IAM role...")
        if not delete_iam_role(config):
            print("Warning: Failed to delete IAM role")
        
        # Delete core + storage policies
        print("\n2. Deleting IAM policies...")
        if not delete_iam_policy(config):
            print("Warning: Failed to delete IAM policy")
        
        print("\n✓ IAM resources deletion completed")
        return True
        
    except Exception as e:
        print(f"Error deleting IAM resources: {e}")
        return False

def delete_local_config():
    """Delete local config.json after AWS resources are removed."""
    print(f"\n{'='*60}")
    print("Deleting local config.json")
    print(f"{'='*60}")

    try:
        if os.path.exists(config_path):
            os.remove(config_path)
            print(f"✓ Deleted {config_path}")
        else:
            print(f"config.json not found (may already be deleted): {config_path}")
        return True
    except OSError as e:
        print(f"Error deleting config.json: {e}")
        return False


def _project_name(config: dict) -> str:
    return config.get("projectName") or "strands-runtime"


def _online_evaluation_config_name(project_name: str) -> str:
    return f"{project_name.replace('-', '_')}_strands_online_eval"


def _evaluation_role_name(project_name: str) -> str:
    return f"AmazonBedrockAgentCoreEvaluationRoleFor{project_name}"


def _guardrail_name(project_name: str) -> str:
    return f"guardrail-for-{project_name.replace('_', '-').lower()}"


def _find_online_evaluation_config(client, config_name: str) -> dict | None:
    next_token = None
    while True:
        params = {}
        if next_token:
            params["nextToken"] = next_token
        response = client.list_online_evaluation_configs(**params)
        for item in response.get("onlineEvaluationConfigs", []):
            if item.get("onlineEvaluationConfigName") == config_name:
                return item
        next_token = response.get("nextToken")
        if not next_token:
            return None


def _find_guardrail_by_name(bedrock_client, name: str) -> dict | None:
    next_token = None
    while True:
        kwargs = {}
        if next_token:
            kwargs["nextToken"] = next_token
        response = bedrock_client.list_guardrails(**kwargs)
        for guardrail in response.get("guardrails", []):
            if guardrail.get("name") == name:
                return guardrail
        next_token = response.get("nextToken")
        if not next_token:
            return None


def _delete_managed_policy(iam_client, policy_arn: str, policy_label: str) -> bool:
    try:
        versions = iam_client.list_policy_versions(PolicyArn=policy_arn)["Versions"]
        for version in versions:
            if not version["IsDefaultVersion"]:
                try:
                    iam_client.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=version["VersionId"],
                    )
                except Exception as e:
                    print(f"Warning: Failed to delete policy version {version['VersionId']}: {e}")
        iam_client.delete_policy(PolicyArn=policy_arn)
        print(f"✓ Deleted IAM policy: {policy_label}")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchEntity", "NoSuchEntityException"):
            print(f"IAM policy not found (may already be deleted): {policy_label}")
            return True
        print(f"Warning: Failed to delete IAM policy {policy_label}: {e}")
        return False
    except Exception as e:
        print(f"Warning: Failed to delete IAM policy {policy_label}: {e}")
        return False


def delete_online_evaluation():
    """Delete online evaluation config and its evaluation IAM role/policy."""
    print(f"\n{'='*60}")
    print("Deleting AgentCore online evaluation")
    print(f"{'='*60}")

    try:
        config = load_config()
        if not config:
            return False

        region = _resolve_region(config)
        project_name = _project_name(config)
        account_id = config.get("accountId")
        config_name = (
            config.get("online_evaluation_config_name")
            or _online_evaluation_config_name(project_name)
        )
        config_id = config.get("online_evaluation_config_id")

        client = boto3.client("bedrock-agentcore-control", region_name=region)

        if not config_id:
            existing = _find_online_evaluation_config(client, config_name)
            if existing:
                config_id = existing.get("onlineEvaluationConfigId")

        if config_id:
            try:
                client.delete_online_evaluation_config(onlineEvaluationConfigId=config_id)
                print(f"✓ Deleted online evaluation config: {config_name} ({config_id})")
            except ClientError as e:
                if e.response["Error"]["Code"] in (
                    "ResourceNotFoundException",
                    "ResourceNotFound",
                ):
                    print(
                        f"Online evaluation config not found "
                        f"(may already be deleted): {config_name}"
                    )
                else:
                    print(f"Warning: Failed to delete online evaluation config: {e}")
        else:
            print(
                f"Online evaluation config not found "
                f"(may already be deleted): {config_name}"
            )

        role_name = _evaluation_role_name(project_name)
        policy_name = f"{role_name}Policy"
        iam_client = boto3.client("iam")

        try:
            attached = iam_client.list_attached_role_policies(RoleName=role_name)
            for policy in attached.get("AttachedPolicies", []):
                try:
                    iam_client.detach_role_policy(
                        RoleName=role_name,
                        PolicyArn=policy["PolicyArn"],
                    )
                    print(f"✓ Detached policy from evaluation role: {policy['PolicyArn']}")
                except ClientError as e:
                    if e.response["Error"]["Code"] not in ("NoSuchEntity", "NoSuchEntityException"):
                        print(f"Warning: Failed to detach {policy['PolicyArn']}: {e}")

            inline = iam_client.list_role_policies(RoleName=role_name)
            for inline_name in inline.get("PolicyNames", []):
                try:
                    iam_client.delete_role_policy(RoleName=role_name, PolicyName=inline_name)
                    print(f"✓ Deleted inline policy: {inline_name}")
                except Exception as e:
                    print(f"Warning: Failed to delete inline policy {inline_name}: {e}")

            iam_client.delete_role(RoleName=role_name)
            print(f"✓ Deleted evaluation IAM role: {role_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchEntity", "NoSuchEntityException"):
                print(f"Evaluation IAM role not found (may already be deleted): {role_name}")
            else:
                print(f"Warning: Failed to delete evaluation IAM role: {e}")

        if account_id:
            policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
            _delete_managed_policy(iam_client, policy_arn, policy_name)
        else:
            print("Warning: accountId missing; skipped evaluation IAM policy deletion")

        print("\n✓ Online evaluation deletion completed")
        return True
    except Exception as e:
        print(f"Warning: Error deleting online evaluation: {e}")
        return True


def delete_bedrock_guardrail():
    """Delete Bedrock Guardrail created by the Strands runtime installer."""
    print(f"\n{'='*60}")
    print("Deleting Bedrock Guardrail")
    print(f"{'='*60}")

    try:
        config = load_config()
        if not config:
            return False

        region = _resolve_region(config)
        project_name = _project_name(config)
        name = config.get("guardrail_name") or _guardrail_name(project_name)
        guardrail_id = config.get("guardrail_id")
        bedrock_client = boto3.client("bedrock", region_name=region)

        if not guardrail_id:
            existing = _find_guardrail_by_name(bedrock_client, name)
            if existing:
                guardrail_id = existing.get("id")

        if not guardrail_id:
            print(f"Guardrail not found (may already be deleted): {name}")
            return True

        try:
            bedrock_client.delete_guardrail(guardrailIdentifier=guardrail_id)
            print(f"✓ Deleted Bedrock Guardrail: {name} ({guardrail_id})")
        except ClientError as e:
            if e.response["Error"]["Code"] in (
                "ResourceNotFoundException",
                "ResourceNotFound",
            ):
                print(f"Guardrail not found (may already be deleted): {name}")
            else:
                print(f"Warning: Failed to delete guardrail: {e}")
                return True

        print("\n✓ Bedrock Guardrail deletion completed")
        return True
    except Exception as e:
        print(f"Warning: Error deleting Bedrock Guardrail: {e}")
        return True


def _monitoring_dashboard_name(config: dict) -> str:
    """Resolve project monitoring dashboard name from config or projectName."""
    name = config.get("cloudwatch_dashboard_name")
    if name:
        return name
    project_name = config.get("projectName", "strands-runtime")
    try:
        from cloudwatch_metrics import dashboard_name

        return dashboard_name(project_name)
    except Exception:
        return f"{str(project_name).replace(' ', '-')}-monitoring"


def _delete_dashboard(client, name: str) -> bool:
    """Delete a single CloudWatch dashboard. Returns True if deleted or already gone."""
    if not name:
        return True
    try:
        client.delete_dashboards(DashboardNames=[name])
        print(f"✓ Deleted CloudWatch dashboard: {name}")
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFound", "ResourceNotFoundException"):
            print(f"CloudWatch dashboard not found (already deleted): {name}")
            return True
        # delete_dashboards returns 200 even when some names are missing; other errors surface here
        print(f"Warning: Failed to delete dashboard '{name}': {e}")
        return False
    except Exception as e:
        print(f"Warning: Failed to delete dashboard '{name}': {e}")
        return False


def delete_cloudwatch_dashboards():
    """Delete CloudWatch monitoring dashboard created by the Strands runtime installer.

    Only removes the project-specific dashboard ({projectName}-monitoring).
    Bedrock-Usage-Dashboard is shared across runtimes and is left intact.
    """
    print(f"\n{'='*60}")
    print("Deleting CloudWatch dashboards")
    print(f"{'='*60}")

    try:
        config = load_config()
        if not config:
            print("Warning: config.json not found; skipping dashboard deletion")
            return True

        region = _resolve_region(config)
        client = boto3.client("cloudwatch", region_name=region)

        project_dashboard = _monitoring_dashboard_name(config)
        _delete_dashboard(client, project_dashboard)

        print("\n✓ CloudWatch dashboard deletion completed")
        return True
    except Exception as e:
        print(f"Warning: Error deleting CloudWatch dashboards: {e}")
        return True


# ============================================================================
# Main Function
# ============================================================================

def main():
    """Main function: Execute the entire uninstallation process."""
    parser = argparse.ArgumentParser(description="AgentCore Runtime Uninstaller")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and proceed with deletion"
    )
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("AgentCore Runtime Uninstallation Script")
    print("="*60)
    
    # Check config.json
    config = load_config()
    if not config:
        print("\nError: Cannot proceed without config.json")
        sys.exit(1)
    
    print(f"Configuration file loaded successfully")
    print(f"  - Project Name: {config.get('projectName')}")
    print(f"  - Region: {config.get('region')}")
    print(f"  - Account ID: {config.get('accountId')}")
    
    # Confirm deletion (skip if --yes flag is provided)
    if not args.yes:
        print("\n" + "="*60)
        print("WARNING: This will delete all resources created by installer.py")
        print("="*60)
        response = input("\nAre you sure you want to continue? (yes/no): ")
        if response.lower() != 'yes':
            print("Uninstallation cancelled.")
            sys.exit(0)
    
    # Execute each step in reverse order of installer creation
    steps = [
        ("Deleting AgentCore online evaluation", delete_online_evaluation),
        ("Deleting Bedrock Guardrail", delete_bedrock_guardrail),
        ("Deleting CloudWatch dashboards", delete_cloudwatch_dashboards),
        ("Deleting AgentCore runtime", delete_agent_runtime),
        ("Deleting ECR repository", delete_ecr_repository),
        ("Deleting IAM role and policy", delete_iam_resources),
    ]
    
    for step_name, step_func in steps:
        if not step_func():
            print(f"\nWarning: Error occurred in step '{step_name}'.")
            print("   Continuing with remaining steps...")

    delete_local_config()

    # Output final results
    print("\n" + "="*60)
    print("Uninstallation process completed!")
    print("="*60)

if __name__ == "__main__":
    main()
