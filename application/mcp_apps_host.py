"""MCP Apps host helpers for ECS: iframe tool proxy for hosted MCP servers (notion-remote)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("mcp_apps_host")

_APP_BY_TOOL: dict[str, dict[str, Any]] = {}


def clear() -> None:
    _APP_BY_TOOL.clear()


def register_tool_app(
    tool_name: str,
    *,
    resource_uri: str,
    html: str,
    mcp_server: str | None = None,
    csp: dict[str, Any] | None = None,
    mime_type: str | None = None,
) -> None:
    if not tool_name or not resource_uri or not html:
        return
    _APP_BY_TOOL[tool_name] = {
        "resourceUri": resource_uri,
        "html": html,
        "mcpServer": mcp_server,
        "csp": csp or {},
        "mimeType": mime_type or "text/html;profile=mcp-app",
        "tool": tool_name,
    }


def get_tool_app(tool_name: str | None) -> dict[str, Any] | None:
    if not tool_name:
        return None
    return _APP_BY_TOOL.get(tool_name)


def build_app_payload(tool_name: str | None, tool_result_text: str) -> dict[str, Any] | None:
    app = get_tool_app(tool_name)
    if not app:
        return None
    return {
        **app,
        "toolResult": {
            "content": [{"type": "text", "text": tool_result_text}],
        },
    }


def normalize_tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" and isinstance(content.get("text"), str):
            return content["text"]
        import json

        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    if isinstance(content, list):
        texts: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif hasattr(block, "text"):
                texts.append(str(getattr(block, "text") or ""))
            else:
                texts.append(str(block))
        return "\n".join(t for t in texts if t)
    return str(content)


# Tools an MCP App iframe may call on the signed-in user's behalf, per remote MCP.
# The iframe is untrusted page content, so this is an explicit allowlist (never
# "whatever the server lists").
NOTION_IFRAME_TOOLS = frozenset(
    {
        "notion-search",
        "notion-fetch",
        "notion-list-recent-pages",
        "notion-list-favorite-pages",
        "notion-list-private-pages",
        "notion-list-shared-pages",
    }
)

# Names an MCP server declares for itself, as older stored mcpApp payloads carried them.
_SERVER_ALIASES: dict[str, str] = {}

# Excalidraw OSS MCP Apps: widget-only tools (model uses read_me / create_view).
EXCALIDRAW_IFRAME_TOOLS = frozenset(
    {
        "export_to_excalidraw",
        "save_checkpoint",
        "read_checkpoint",
    }
)
EXCALIDRAW_MCP_URL = "https://mcp.excalidraw.com/mcp"

# server name (as used in the mcpApp payload) -> (auth module, allowed tools)
_REMOTE_OAUTH_MCPS: dict[str, tuple[str, frozenset[str]]] = {
    "notion-remote": ("notion_auth", NOTION_IFRAME_TOOLS),
}
# Public Streamable HTTP MCPs (no OAuth). URL + iframe tool allowlist.
_PUBLIC_HTTP_MCPS: dict[str, tuple[str, frozenset[str]]] = {
    "excalidraw": (EXCALIDRAW_MCP_URL, EXCALIDRAW_IFRAME_TOOLS),
}
_ALLOWED_TOOLS: dict[str, frozenset[str]] = {
    **{k: v[1] for k, v in _REMOTE_OAUTH_MCPS.items()},
    **{k: v[1] for k, v in _PUBLIC_HTTP_MCPS.items()},
}


@asynccontextmanager
async def remote_session(server: str, user_id: str | None):
    """Open an MCP ClientSession to a hosted MCP for iframe resource/tool proxying.

    OAuth servers (Notion) use the user's own token; public servers (excalidraw)
    need no credentials.
    """
    import asyncio
    import importlib

    if server not in _REMOTE_OAUTH_MCPS and server not in _PUBLIC_HTTP_MCPS:
        raise ValueError(f"No remote MCP session for server: {server}")
    if not user_id:
        raise ValueError("Sign in required")

    import mcp
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    if server in _PUBLIC_HTTP_MCPS:
        url, _allowed = _PUBLIC_HTTP_MCPS[server]
        http = create_mcp_http_client(timeout=None)
        async with http:
            async with streamable_http_client(url, http_client=http) as streams:
                async with mcp.ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield session
        return

    module_name, _allowed = _REMOTE_OAUTH_MCPS[server]
    try:
        auth = importlib.import_module(f"application.{module_name}")
    except ImportError:  # pragma: no cover - flat layout
        auth = importlib.import_module(module_name)

    token = await asyncio.to_thread(auth.get_access_token, user_id)
    if not token:
        raise ValueError(f"{server} is not connected. Connect it from the MCP menu.")

    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with http:
        async with streamable_http_client(auth.PROVIDER.mcp_url, http_client=http) as streams:
            async with mcp.ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def _call_remote_oauth_tool(
    server: str,
    user_id: str | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Call a hosted MCP (Notion) with the signed-in user's OAuth token."""
    allowed = _ALLOWED_TOOLS[server]
    if tool_name not in allowed:
        raise ValueError(f"{server} tool not allowed from MCP Apps: {tool_name}")
    if not user_id:
        raise ValueError("Sign in required")
    async with remote_session(server, user_id) as session:
        result = await session.call_tool(tool_name, arguments)
    # Keep the whole result (structuredContent, image blocks, _meta): UIs read them.
    return result.model_dump(by_alias=True, exclude_none=True, mode="json")


def normalize_server(name: str | None) -> str:
    """Canonical server key (lower-case, declared-name aliases resolved)."""
    server = (name or "").strip().lower()
    return _SERVER_ALIASES.get(server, server)


async def call_mcp_tool(
    mcp_server: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Proxy an MCP App iframe ``tools/call`` to a hosted MCP with the user's OAuth token."""
    arguments = arguments or {}
    server = normalize_server(mcp_server)
    name = (tool_name or "").strip()

    if server in _ALLOWED_TOOLS:
        return await _call_remote_oauth_tool(server, user_id, name, arguments)
    raise ValueError(
        "ECS mcp-apps proxy only supports notion-remote and excalidraw "
        f"(got mcpServer={mcp_server!r})"
    )
