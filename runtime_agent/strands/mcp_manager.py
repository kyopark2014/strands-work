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

"""MCP client manager and Streamable HTTP helpers for the Strands runtime agent."""

from __future__ import annotations

import contextlib
import logging
import os
from contextlib import contextmanager
from typing import Dict, List, Optional

import chat
import mcp_config
from mcp import stdio_client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from strands.tools.mcp import MCPClient

logger = logging.getLogger("strands-agent")


@contextlib.asynccontextmanager
async def _streamable_http_with_auth(
    url: str,
    auth,
    *,
    terminate_on_close: bool = True,
):
    """Streamable HTTP MCP with SigV4 auth (e.g. gateway-websearch)."""
    client = create_mcp_http_client(auth=auth)
    async with client:
        async with streamable_http_client(
            url,
            http_client=client,
            terminate_on_close=terminate_on_close,
        ) as streams:
            yield streams


@contextlib.asynccontextmanager
async def _streamable_http_with_headers(
    url: str,
    headers: dict[str, str],
    *,
    terminate_on_close: bool = True,
):
    """Custom headers for Streamable HTTP MCP (MCP SDK 2.x streamable_http_client)."""
    client = create_mcp_http_client(headers=headers)
    async with client:
        async with streamable_http_client(
            url,
            http_client=client,
            terminate_on_close=terminate_on_close,
        ) as streams:
            yield streams


class MCPClientManager:
    def __init__(self):
        self.clients: Dict[str, MCPClient] = {}
        self.client_configs: Dict[str, dict] = {}  # Store client configurations
        self._persistent_stack: Optional[contextlib.ExitStack] = None
        self._persistent_client_names: List[str] = []
        # Last start_agent_clients() request + names that failed for that request.
        # Reuse compares against the request list (not only started names) so one
        # broken MCP (e.g. uvx) does not force a full stack restart.
        self._last_requested_names: List[str] = []
        self._failed_client_names: set[str] = set()

    def add_stdio_client(self, name: str, command: str, args: List[str], env: dict[str, str] = {}) -> None:
        """Add a new MCP client configuration (lazy initialization)"""
        new_config = {
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": env
        }
        # Drop cached client when env/command changes so AGENTCORE_USER_ID etc. take effect.
        old_config = self.client_configs.get(name)
        if name in self.clients and old_config != new_config:
            logger.info(f"Invalidating cached MCP client for {name} due to config change")
            del self.clients[name]
        self.client_configs[name] = new_config
    
    def add_streamable_client(
        self,
        name: str,
        url: str,
        headers: dict[str, str] = {},
        auth_region: str | None = None,
        auth_kind: str | None = None,
    ) -> None:
        """Add a new MCP client configuration (lazy initialization)"""
        self.client_configs[name] = {
            "transport": "streamable_http",
            "url": url,
            "headers": headers,
            "auth_region": auth_region,
            "auth_kind": auth_kind,
        }
    
    def get_client(self, name: str) -> Optional[MCPClient]:
        """Get or create MCP client (lazy initialization)"""
        if name not in self.client_configs:
            logger.warning(f"No configuration found for MCP client: {name}")
            return None
            
        if name not in self.clients:
            # Create client on first use
            config = self.client_configs[name]
            logger.info(f"Creating {name} MCP client with config: {config}")
            try:
                if "transport" in config and config["transport"] == "streamable_http":
                    try:
                        url = config["url"]
                        hdrs = config.get("headers") or {}
                        auth_region = config.get("auth_region")
                        if config.get("auth_kind") == "notion_bearer":
                            # Token is read per request (chat / env), so refreshed
                            # tokens apply without rebuilding the client.
                            import notion_bearer_auth
                            auth = notion_bearer_auth.NotionBearerAuth()
                            self.clients[name] = MCPClient(
                                lambda u=url, a=auth: _streamable_http_with_auth(
                                    u, a, terminate_on_close=True
                                )
                            )
                        elif auth_region:
                            import agentcore_sigv4_auth
                            auth = agentcore_sigv4_auth.AgentCoreSigV4Auth(region=auth_region)
                            self.clients[name] = MCPClient(
                                lambda u=url, a=auth: _streamable_http_with_auth(
                                    u, a, terminate_on_close=True
                                )
                            )
                        elif hdrs:
                            # Build httpx inside the MCP background thread's event loop.
                            # Pre-creating AsyncClient on the main thread binds it to the wrong loop.
                            self.clients[name] = MCPClient(
                                lambda u=url, h=dict(hdrs): _streamable_http_with_headers(
                                    u, h, terminate_on_close=True
                                )
                            )
                        else:
                            self.clients[name] = MCPClient(
                                lambda u=url: streamable_http_client(u)
                            )
                    except Exception as http_error:
                        logger.error(
                            "Failed to create streamable HTTP client for %s: %s",
                            name,
                            type(http_error).__name__,
                        )
                        if (
                            "403" in str(http_error)
                            or "Forbidden" in str(http_error)
                            or "MCPClientInitializationError" in str(http_error)
                            or "client initialization failed" in str(http_error)
                        ):
                            logger.error(
                                "Authentication failed for %s; skipping client creation",
                                name,
                            )
                            return None
                        raise http_error
                else:
                    self.clients[name] = MCPClient(lambda: stdio_client(
                        StdioServerParameters(
                            command=config["command"], 
                            args=config["args"], 
                            env=config["env"]
                        )
                    ))
                
                logger.info(f"Successfully created MCP client: {name}")
            except Exception as exc:
                logger.error(f"Failed to create MCP client {name}: {exc}")
                logger.error(f"Exception type: {type(exc)}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")
                return None
        else:
            # Never tear down a live persistent session just to return the handle.
            if (
                self._persistent_stack
                and name in self._persistent_client_names
            ):
                return self.clients[name]
            # Temporary (non-persistent) path: stop an existing session before re-enter.
            try:
                client = self.clients[name]
                if hasattr(client, '_session') and client._session is not None:
                    logger.info(f"Stopping existing session for client: {name}")
                    try:
                        client.stop()
                    except Exception as stop_error:
                        # Ignore 404 errors during session termination (common with AWS Bedrock AgentCore)
                        if "404" in str(stop_error) or "Not Found" in str(stop_error):
                            logger.info(f"Session already terminated for {name} (404 expected)")
                        else:
                            logger.warning(f"Error stopping existing client session for {name}: {stop_error}")
            except Exception as exc:
                logger.warning(f"Error checking client session for {name}: {exc}")
        return self.clients[name]
    
    def remove_client(self, name: str) -> None:
        """Remove an MCP client"""
        if name in self.clients:
            del self.clients[name]
        if name in self.client_configs:
            del self.client_configs[name]
        self._failed_client_names.discard(name)

    def is_client_failed(self, name: str) -> bool:
        """True if the last start attempt recorded a failure for this client name."""
        return name in self._failed_client_names

    def _all_mcp_sessions_active(self, client_names: List[str]) -> bool:
        """Return True if every named Strands MCPClient has an active background session."""
        for name in client_names:
            client = self.clients.get(name)
            if client is None or not client._is_session_active():
                return False
        return True

    def _runnable_client_names(self, client_names: List[str]) -> List[str]:
        """Requested names that have config and are not known-failed for this request."""
        return [
            name
            for name in client_names
            if name in self.client_configs and name not in self._failed_client_names
        ]

    def start_agent_clients(self, client_names: List[str]) -> bool:
        """Start MCP clients persistently. Restarts when the request set changes or sessions die.

        Successfully started names may be a subset of ``client_names`` when some MCPs fail.
        A later call with the same request reuses that subset instead of restarting.
        """
        requested = list(client_names)

        if (
            self._persistent_stack
            and self._last_requested_names == requested
            and self._persistent_client_names
            and self._all_mcp_sessions_active(self._persistent_client_names)
        ):
            logger.info(
                "Persistent MCP clients already running: %s (failed=%s)",
                self._persistent_client_names,
                sorted(self._failed_client_names),
            )
            return False

        if (
            self._persistent_stack
            and self._last_requested_names == requested
            and self._persistent_client_names
        ):
            logger.warning(
                "MCP request unchanged but session(s) inactive; restarting persistent stack."
            )

        if self._last_requested_names != requested:
            self._failed_client_names.clear()

        self.stop_agent_clients()
        self._last_requested_names = requested

        if not requested:
            return False

        logger.info(f"Starting persistent MCP clients: {requested}")
        self._persistent_stack = contextlib.ExitStack()

        try:
            started: List[str] = []
            for name in requested:
                if name in self._failed_client_names:
                    logger.info("Skipping previously failed MCP client: %s", name)
                    continue
                client = self.get_client(name)
                if not client:
                    logger.warning(
                        f"MCP client not configured for {name!r}; skipping. "
                        "Check init_mcp_clients and mcp_config."
                    )
                    self._failed_client_names.add(name)
                    continue
                try:
                    self._persistent_stack.enter_context(client)
                    started.append(name)
                    logger.info(f"client started: {name}")
                except Exception as exc:
                    # One broken MCP (uvx/PyPI/gateway) must not abort the whole stream.
                    logger.error(
                        "Skipping failed MCP client %r during start: %s",
                        name,
                        type(exc).__name__,
                        exc_info=True,
                    )
                    self._failed_client_names.add(name)
                    # Drop broken client so a future request can recreate it.
                    if name in self.clients:
                        del self.clients[name]
                    continue
            if not started:
                self.stop_agent_clients()
                logger.warning(
                    "No MCP clients started successfully for %s; continuing without MCP tools",
                    requested,
                )
                return False
            self._persistent_client_names = started
            return True
        except Exception:
            self.stop_agent_clients()
            raise
    
    def stop_agent_clients(self):
        """Stop all persistent MCP clients."""
        if self._persistent_stack:
            logger.info(f"Stopping persistent MCP clients: {self._persistent_client_names}")
            try:
                self._persistent_stack.close()
            except Exception as exc:
                logger.warning(f"Error stopping persistent clients: {exc}")
            self._persistent_stack = None
            self._persistent_client_names = []
        # Allow the next start (e.g. after needs_agent rebuild) to retry failures.
        self._last_requested_names = []
        self._failed_client_names.clear()
    
    @contextmanager
    def get_active_clients(self, active_clients: List[str]):
        """Manage active clients context.

        Prefers the persistent stack when every runnable requested client is already
        started there (including single-server list_tools calls).
        """
        needed = self._runnable_client_names(active_clients)
        if (
            self._persistent_stack
            and needed
            and set(needed).issubset(set(self._persistent_client_names))
            and self._all_mcp_sessions_active(needed)
        ):
            logger.info("Reusing MCP clients: %s", needed)
            yield
            return

        if not needed:
            yield
            return

        active_contexts = []
        try:
            for client_name in needed:
                client = self.get_client(client_name)
                if client:
                    # Ensure client is not already running
                    try:
                        if hasattr(client, '_session') and client._session is not None:
                            logger.info(f"Stopping existing session for client: {client_name}")
                            try:
                                client.stop()
                            except Exception as stop_error:
                                # Ignore 404 errors during session termination (common with AWS Bedrock AgentCore)
                                if "404" in str(stop_error) or "Not Found" in str(stop_error):
                                    logger.info(f"Session already terminated for {client_name} (404 expected)")
                                else:
                                    logger.warning(f"Error stopping existing session for {client_name}: {stop_error}")
                    except Exception as exc:
                        logger.warning(f"Error checking existing session for {client_name}: {exc}")
                    
                    active_contexts.append(client)

            # logger.info(f"active_contexts: {active_contexts}")
            if active_contexts:
                with contextlib.ExitStack() as stack:
                    for client in active_contexts:
                        try:
                            stack.enter_context(client)
                        except Exception as exc:
                            logger.error(f"Error entering context for client: {exc}")
                            
                            # Check if this is a 403 error and try to refresh bearer token
                            logger.info(f"Error details: {type(exc).__name__}: {str(exc)}")
                            if "403" in str(exc) or "Forbidden" in str(exc) or "MCPClientInitializationError" in str(exc) or "client initialization failed" in str(exc):
                                logger.error(
                                    "Authentication failed entering MCP client context; skipping client"
                                )
                            
                            # Try to stop the client if it's already running
                            try:
                                if hasattr(client, 'stop'):
                                    try:
                                        client.stop()
                                    except Exception as stop_error:
                                        # Ignore 404 errors during session termination
                                        if "404" in str(stop_error) or "Not Found" in str(stop_error):
                                            logger.info(f"Session already terminated (404 expected)")
                                        else:
                                            logger.warning(f"Error stopping client: {stop_error}")
                            except Exception as stop_context_error:
                                logger.warning(
                                    "Failed while preparing to stop MCP client %r: %s: %s",
                                    client,
                                    type(stop_context_error).__name__,
                                    stop_context_error,
                                )
                            # Skip this MCP client so one broken tool does not kill the stream.
                            logger.error(
                                "Skipping failed MCP client and continuing with remaining tools"
                            )
                            continue
                    yield
            else:
                yield
        except Exception as exc:
            logger.error(f"Error in MCP client context: {exc}")
            raise


# Initialize MCP client manager
mcp_manager = MCPClientManager()


def init_mcp_clients(mcp_servers: list):
    for tool in mcp_servers:
        logger.info(f"Initializing MCP client for tool: {tool}")
        config = mcp_config.load_config(tool)
        # logger.info(f"config: {config}")

        # Skip if config is empty or doesn't have mcpServers
        if not config or "mcpServers" not in config:
            logger.warning(f"No configuration found for tool: {tool}")
            continue

        # Get the first key from mcpServers
        server_key = next(iter(config["mcpServers"]))
        server_config = config["mcpServers"][server_key]
        
        if "type" in server_config and server_config["type"] == "streamable_http":
            name = tool  # Use tool name as client name
            url = server_config["url"]
            headers = server_config.get("headers", {})
            auth_region = None
            auth_kind = None
            if server_config.get("auth_type") == "aws_sigv4":
                auth_region = server_config.get("auth_region", "us-west-2")
            elif server_config.get("auth_type") == "notion_bearer":
                auth_kind = "notion_bearer"
                notion_token = (
                    getattr(chat, "notion_access_token", None)
                    or os.environ.get("NOTION_REMOTE_ACCESS_TOKEN")
                    or ""
                ).strip()
                if not notion_token:
                    logger.warning(
                        "notion-remote MCP skipped: notion_access_token missing "
                        "(connect Notion in the web UI)"
                    )
                    mcp_manager.remove_client(name)
                    continue
            logger.info(f"Adding MCP client - name: {name}, url: {url}, headers: {headers}")
                
            try:                
                mcp_manager.add_streamable_client(
                    name, url, headers, auth_region=auth_region, auth_kind=auth_kind
                )
                logger.info(f"Successfully added streamable MCP client for {name}")
            except Exception as exc:
                logger.error(f"Failed to add streamable MCP client for {name}: {exc}")
                
        else:
            name = tool  # Use tool name as client name
            command = server_config["command"]
            args = server_config["args"]
            env = dict(server_config.get("env") or {})
            # UI may send "knowledge base"; load_config maps it to server_key "kb-retriever".
            if name in (
                "memory",
                "graph memory",
                "wiki",
                "kb-retriever",
                "knowledge base",
            ) or server_key in (
                "memory",
                "graph memory",
                "wiki",
                "kb-retriever",
            ):
                env["AGENTCORE_USER_ID"] = chat.user_id if chat.user_id else "default"
            logger.info(f"name: {name}, command: {command}, args: {args}, env: {env}")

            # Skip if command is a file path and the executable doesn't exist
            cmd_path = os.path.expanduser(command) if isinstance(command, str) else str(command)
            if "/" in cmd_path or (isinstance(command, str) and command.startswith("~")):
                if not os.path.isfile(cmd_path):
                    logger.warning(f"Skipping {name}: executable not found at {cmd_path}")
                    continue

            try:
                mcp_manager.add_stdio_client(name, command, args, env)
                logger.info(f"Successfully added {name} MCP client")
            except Exception as exc:
                logger.error(f"Failed to add stdio MCP client for {name}: {exc}")
                continue
