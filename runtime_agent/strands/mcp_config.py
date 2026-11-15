import logging
import sys
import json
import utils
import os
import shutil
import boto3

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("mcp-config")

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, "config.json")

config = utils.load_config()
logger.info(f"config: {config}")

region = config["region"] if "region" in config else "us-west-2"
projectName = config["projectName"] if "projectName" in config else "mcp"
workingDir = os.path.dirname(os.path.abspath(__file__))
# 상위 디렉토리의 contents 폴더 경로 추가
parent_dir = os.path.dirname(workingDir)
contents_dir = os.path.join(parent_dir, "contents")
logger.info(f"workingDir: {workingDir}")
logger.info(f"contents_dir: {contents_dir}")


def _stdio_env(**extra: str) -> dict[str, str]:
    """Merge parent process env so stdio MCP children keep KNOWLEDGE_BASE_ID / AWS creds."""
    env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    env.update({k: v for k, v in extra.items() if v is not None})
    # Prefer config.json values when env vars were not injected on the Runtime.
    if not env.get("KNOWLEDGE_BASE_ID") and config.get("knowledge_base_id"):
        env["KNOWLEDGE_BASE_ID"] = str(config["knowledge_base_id"])
    if not env.get("PROJECT_NAME") and config.get("projectName"):
        env["PROJECT_NAME"] = str(config["projectName"])
    if not env.get("AWS_REGION") and config.get("region"):
        env["AWS_REGION"] = str(config["region"])
        env.setdefault("AWS_DEFAULT_REGION", str(config["region"]))
    # Ensure MCP children see runtime config even if parent env was incomplete
    # at process start (config.json is dockerignored; APP_CONFIG_JSON is source).
    if not env.get("APP_CONFIG_JSON"):
        try:
            fresh = utils.load_config()
            env["APP_CONFIG_JSON"] = json.dumps(
                {k: v for k, v in fresh.items() if v not in (None, "")},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except Exception as e:
            logger.warning("Could not inject APP_CONFIG_JSON for MCP child: %s", e)
    return env

def get_agentcore_gateway_mcp_url(gateway_name: str, gateway_region: str) -> str | None:
    # Prefer URL already written by installer/CDK/Terraform (avoids ListGateways).
    configured_url = (
        config.get("agentcore_websearch_gateway_url")
        or os.environ.get("agentcore_websearch_gateway_url")
        or os.environ.get("AGENTCORE_WEBSEARCH_GATEWAY_URL")
        or ""
    ).strip()
    if configured_url:
        logger.info(f"Using configured AgentCore gateway URL for {gateway_name}")
        return configured_url.rstrip("/")

    # Resolve by name: project gateway is usually `{projectName}`, not "gateway-websearch".
    names_to_try = []
    for candidate in (
        gateway_name,
        config.get("agentcore_websearch_gateway_name"),
        config.get("projectName"),
        projectName,
        "gateway-websearch",
    ):
        if candidate and candidate not in names_to_try:
            names_to_try.append(str(candidate))

    client = boto3.client("bedrock-agentcore-control", region_name=gateway_region)
    try:
        items = []
        next_token = None
        while True:
            kwargs = {}
            if next_token:
                kwargs["nextToken"] = next_token
            response = client.list_gateways(**kwargs)
            items.extend(response.get("items") or response.get("gateways") or [])
            next_token = response.get("nextToken")
            if not next_token:
                break

        for item in items:
            item_name = item.get("name") or item.get("gatewayName")
            if item_name not in names_to_try:
                continue

            gateway_id = item.get("gatewayId") or item.get("gatewayIdentifier")
            gateway = client.get_gateway(gatewayIdentifier=gateway_id)
            return gateway["gatewayUrl"].rstrip("/")
    except Exception as e:
        logger.error(f"Error resolving AgentCore gateway URL for {gateway_name}: {e}")

    return None


def load_config(mcp_type):
    if mcp_type == "knowledge base":
        mcp_type = "kb-retriever"
    elif mcp_type == "aws documentation":
        mcp_type = "aws_documentation"    
    elif mcp_type == "trade info":
        mcp_type = "trade_info"
    elif mcp_type == "weather":
        mcp_type = "korea_weather"
    elif mcp_type == "image generation":
        mcp_type = "image_generation"
    
    if mcp_type == "use-aws":
        return {
            "mcpServers": {
                "use-aws": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_use_aws.py"]
                }
            }
        }

    elif mcp_type == "aws_documentation":
        # Prefer image-preinstalled binary over uvx (avoids PyPI resolve/install on cold start).
        # Dockerfile: pip install awslabs.aws-documentation-mcp-server
        env = {"FASTMCP_LOG_LEVEL": "ERROR"}
        command = shutil.which("awslabs.aws-documentation-mcp-server")
        args: list[str] = []
        if not command:
            logger.warning(
                "awslabs.aws-documentation-mcp-server is not preinstalled; "
                "falling back to uvx (slow on cold start). "
                "Install with: pip install awslabs.aws-documentation-mcp-server "
                "(or: uv tool install awslabs.aws-documentation-mcp-server)"
            )
            command = "uvx"
            args = ["awslabs.aws-documentation-mcp-server@latest"]
        return {
            "mcpServers": {
                "awslabs.aws-documentation-mcp-server": {
                    "command": command,
                    "args": args,
                    "env": env,
                }
            }
        }

    elif mcp_type == "kb-retriever":
        return {
            "mcpServers": {
                "kb-retriever": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_retrieve.py"],
                    # AGENTCORE_USER_ID is injected at runtime in init_mcp_clients()
                    "env": _stdio_env(PYTHONPATH=workingDir),
                }
            }
        }    

    elif mcp_type == "trade_info":
        return {
            "mcpServers": {
                "trade-info": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_trade_info.py"
                    ]
                }
            }
        }

    elif mcp_type == "korea_weather":
        return {
            "mcpServers": {
                "korea-weather": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_korea_weather.py"],
                }
            }
        }
    
    elif mcp_type == "web_fetch":
        # Prefer image-preinstalled package over npx (avoids NAT/npm cold start).
        command = shutil.which("mcp-server-fetch-typescript")
        args: list[str] = []
        if not command:
            local_entry = os.path.join(
                workingDir,
                "node_modules",
                "mcp-server-fetch-typescript",
                "build",
                "index.js",
            )
            if os.path.isfile(local_entry):
                command = "node"
                args = [local_entry]
            else:
                logger.warning(
                    "mcp-server-fetch-typescript is not preinstalled; "
                    "falling back to npx (slow on cold start). "
                    "Install with: npm install mcp-server-fetch-typescript"
                )
                command = "npx"
                args = ["-y", "mcp-server-fetch-typescript"]
        return {
            "mcpServers": {
                "web_fetch": {
                    "command": command,
                    "args": args,
                }
            }
        }
    
    elif mcp_type == "image_generation":
        return {
            "mcpServers": {
                "imageGeneration": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_image_generation.py"
                    ]
                }
            }
        }

    elif mcp_type == "websearch":
        gateway_region = (
            config.get("agentcore_websearch_gateway_region")
            or os.environ.get("AGENTCORE_WEBSEARCH_GATEWAY_REGION")
            or "us-east-1"
        )
        gateway_name = (
            config.get("agentcore_websearch_gateway_name")
            or config.get("projectName")
            or projectName
            or "gateway-websearch"
        )
        gateway_url = get_agentcore_gateway_mcp_url(gateway_name, gateway_region)
        if not gateway_url:
            logger.info(
                "AgentCore gateway websearch MCP skipped: "
                f"{gateway_name} not found in {gateway_region}."
            )
            return {}
        return {
            "mcpServers": {
                "gateway-websearch": {
                    "type": "streamable_http",
                    "url": gateway_url,
                    "auth_type": "aws_sigv4",
                    "auth_region": gateway_region,
                    "auth_service": "bedrock-agentcore",
                }
            }
        }

    elif mcp_type == "noaa":
        return {
            "mcpServers": {
                "noaa-energy-news": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_noaa.py"],
                }
            }
        }

    elif mcp_type == "memory":
        return {
            "mcpServers": {
                "memory": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_memory.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in init_mcp_clients()
                    },
                }
            }
        }

    elif mcp_type == "graph memory":
        return {
            "mcpServers": {
                "graph memory": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_graph_memory.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in init_mcp_clients()
                    },
                }
            }
        }

    elif mcp_type == "wiki":
        return {
            "mcpServers": {
                "wiki": {
                    "command": "python",
                    "args": [f"{workingDir}/mcp_server_wiki.py"],
                    "env": {
                        "PYTHONPATH": workingDir,
                        # AGENTCORE_USER_ID is injected at runtime in init_mcp_clients()
                    },
                }
            }
        }

    elif mcp_type == "pubmed":
        return {
            "mcpServers": {
                "pubmed": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_pubmed.py"
                    ]
                }
            }
        }

    elif mcp_type == "chembl":
        return {
            "mcpServers": {
                "chembl": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_chembl.py"
                    ]
                }
            }
        }

    elif mcp_type == "clinicaltrial":
        return {
            "mcpServers": {
                "clinicaltrial": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_clinicaltrial.py"
                    ]
                }
            }
        }

    elif mcp_type == "arxiv-manual":
        return {
            "mcpServers": {
                "arxiv-manual": {
                    "command": "python",
                    "args": [
                        f"{workingDir}/mcp_server_arxiv.py"
                    ]
                }
            }
        }

    elif mcp_type in ("notion-remote", "notion remote", "notion_remote"):
        # Notion hosted MCP (OAuth bearer injected per request; see notion_bearer_auth.py).
        return {
            "mcpServers": {
                "notion-remote": {
                    "type": "streamable_http",
                    "url": "https://mcp.notion.com/mcp",
                    "auth_type": "notion_bearer",
                }
            }
        }

    elif mcp_type in ("excalidraw", "excalidraw-mcp", "excalidraw mcp"):
        # Open-source Excalidraw MCP Apps (no auth). Diagram UI via ui://excalidraw/...
        # https://mcp.excalidraw.com/mcp — Plus workspace MCP is a different endpoint.
        return {
            "mcpServers": {
                "excalidraw": {
                    "type": "streamable_http",
                    "url": "https://mcp.excalidraw.com/mcp",
                }
            }
        }

def load_selected_config(mcp_servers: dict):
    logger.info(f"mcp_servers: {mcp_servers}")
    
    loaded_config = {}
    for server in mcp_servers:
        config = load_config(server)
        if config:
            loaded_config.update(config["mcpServers"])
    return {
        "mcpServers": loaded_config
    }
