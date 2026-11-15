"""MCP Apps host helpers: cache ui:// HTML for AgentCore runtime tool results."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mcp_apps_host")

# tool_name -> app descriptor
_APP_BY_TOOL: dict[str, dict[str, Any]] = {}


def clear() -> None:
    _APP_BY_TOOL.clear()


# MCP servers whose `ui://` resources the web host reads itself (GET /api/mcp-apps/resource),
# like a real MCP Apps host does with resources/read. For these the tool result carries
# only the resource URI, not the HTML, so a 300KB UI is not repeated in every message.
HOST_FETCHED_SERVERS = frozenset({"notion-remote", "excalidraw"})


def register_tool_app(
    tool_name: str,
    *,
    resource_uri: str,
    html: str | None = None,
    mcp_server: str | None = None,
    csp: dict[str, Any] | None = None,
    mime_type: str | None = None,
) -> None:
    if not tool_name or not resource_uri:
        return
    if not html and (mcp_server or "") not in HOST_FETCHED_SERVERS:
        return  # nothing the host could render
    _APP_BY_TOOL[tool_name] = {
        "resourceUri": resource_uri,
        "html": html,
        "mcpServer": mcp_server,
        "csp": csp or {},
        "mimeType": mime_type or "text/html;profile=mcp-app",
        "tool": tool_name,
    }
    logger.info(
        "Registered MCP App UI for tool=%s uri=%s html_len=%s",
        tool_name,
        resource_uri,
        len(html or ""),
    )


def get_tool_app(tool_name: str | None) -> dict[str, Any] | None:
    if not tool_name:
        return None
    return _APP_BY_TOOL.get(tool_name)


def build_app_payload(
    tool_name: str | None,
    tool_result_text: str,
    structured_content: Any = None,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    app = get_tool_app(tool_name)
    if not app:
        return None
    tool_result: dict[str, Any] = {
        "content": [{"type": "text", "text": tool_result_text}],
    }
    # MCP Apps UIs (e.g. Canva) read the tool's ``structuredContent`` directly.
    if structured_content is not None:
        tool_result["structuredContent"] = structured_content
    payload: dict[str, Any] = {**app, "toolResult": tool_result}
    if payload.get("mcpServer") in HOST_FETCHED_SERVERS:
        # The web host reads the ui:// resource itself (and applies its _meta.ui.csp).
        payload.pop("html", None)
        payload.pop("csp", None)
    if isinstance(tool_input, dict):
        payload["toolInput"] = tool_input
    return payload


def extract_ui_resource_uri(tool_metadata: dict[str, Any] | None) -> str | None:
    """Read `_meta.ui.resourceUri` from MCP tool metadata (``{"mcp": {"tool": {"_meta": ...}}}``)."""
    if not isinstance(tool_metadata, dict):
        return None
    mcp = tool_metadata.get("mcp")
    if not isinstance(mcp, dict):
        return None
    tool_meta = mcp.get("tool")
    if not isinstance(tool_meta, dict):
        return None
    meta = tool_meta.get("_meta")
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui")
    if isinstance(ui, dict):
        uri = ui.get("resourceUri")
        if isinstance(uri, str) and uri.startswith("ui://"):
            return uri
    legacy = meta.get("ui/resourceUri")
    if isinstance(legacy, str) and legacy.startswith("ui://"):
        return legacy
    return None


def is_app_only_tool(tool_metadata: dict[str, Any] | None) -> bool:
    """True when a tool is meant for the MCP App iframe only (``_meta.ui.visibility``
    omits ``"model"``). Such tools are served through the host proxy and are hidden
    from the model so it does not call UI plumbing (e.g. telemetry) directly."""
    if not isinstance(tool_metadata, dict):
        return False
    mcp = tool_metadata.get("mcp")
    tool_meta = mcp.get("tool") if isinstance(mcp, dict) else None
    meta = tool_meta.get("_meta") if isinstance(tool_meta, dict) else None
    ui = meta.get("ui") if isinstance(meta, dict) else None
    visibility = ui.get("visibility") if isinstance(ui, dict) else None
    return isinstance(visibility, list) and bool(visibility) and "model" not in visibility


def extract_mcp_server_name(tool_metadata: dict[str, Any] | None) -> str | None:
    if not isinstance(tool_metadata, dict):
        return None
    mcp = tool_metadata.get("mcp")
    if not isinstance(mcp, dict):
        return None
    server = mcp.get("server")
    if isinstance(server, dict):
        name = server.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _strands_tool_meta(tool: Any) -> dict[str, Any]:
    """`_meta` of the underlying MCP tool wrapped by a Strands ``MCPAgentTool``."""
    mcp_tool = getattr(tool, "mcp_tool", None)
    if mcp_tool is None:
        return {}
    meta = getattr(mcp_tool, "meta", None)
    if meta is None:
        meta = getattr(mcp_tool, "_meta", None)
    if meta is None and isinstance(mcp_tool, dict):
        meta = mcp_tool.get("_meta") or mcp_tool.get("meta")
    return meta if isinstance(meta, dict) else {}


def _strands_tool_metadata(tool: Any) -> dict[str, Any]:
    """Adapt a Strands MCP tool to the LangChain-style metadata the helpers above read."""
    return {"mcp": {"tool": {"_meta": _strands_tool_meta(tool)}}}


def is_app_only_strands_tool(tool: Any) -> bool:
    return is_app_only_tool(_strands_tool_metadata(tool))


# Local stdio MCP Apps whose UI HTML ships with the runtime (no resources/read on
# Strands MCPClient). Includes UI/mcp.list aliases ("korea_weather", "weather") and
# the mcpServers key ("korea-weather") from mcp_config.load_config().
_LOCAL_STDIO_APP_ALIASES: dict[str, str] = {
    "korea-weather": "korea-weather",
    "korea_weather": "korea-weather",
    "weather": "korea-weather",
}
_LOCAL_STDIO_APP_SERVERS = frozenset(_LOCAL_STDIO_APP_ALIASES)


def _local_stdio_app_ui(server_name: str) -> tuple[str, str, dict[str, Any]] | None:
    """Return ``(view_uri, html, csp)`` for a local stdio MCP Apps server."""
    canonical = _LOCAL_STDIO_APP_ALIASES.get(server_name)
    if canonical == "korea-weather":
        try:
            from mcp_server_korea_weather import VIEW_URI, WEATHER_HTML
        except ImportError as exc:
            logger.warning("korea-weather UI HTML unavailable: %s", exc)
            return None
        return (
            VIEW_URI,
            WEATHER_HTML,
            {"resourceDomains": ["https://www.weather.go.kr"]},
        )
    return None


def cache_apps_from_strands_tools(tools: list[Any], *, server_name: str) -> int:
    """Map tools that declare ``_meta.ui.resourceUri`` to their MCP App UI.

    Strands' ``MCPClient`` lists tools synchronously and exposes no resource reader on the
    runtime side. Host-fetched servers (notion-remote, excalidraw) register URI-only
    mappings; the web host reads ``ui://`` via ``GET /api/mcp-apps/resource``. Local
    stdio Apps servers (korea-weather) embed the shipped HTML in the payload instead.
    """
    registered = 0

    if server_name in HOST_FETCHED_SERVERS:
        for tool in tools:
            uri = extract_ui_resource_uri(_strands_tool_metadata(tool))
            if not uri:
                continue
            name = getattr(tool, "tool_name", None) or getattr(tool, "name", None)
            register_tool_app(name, resource_uri=uri, mcp_server=server_name)
            registered += 1
        return registered

    if server_name in _LOCAL_STDIO_APP_SERVERS:
        local = _local_stdio_app_ui(server_name)
        if not local:
            return 0
        view_uri, html, csp = local
        for tool in tools:
            uri = extract_ui_resource_uri(_strands_tool_metadata(tool)) or view_uri
            name = getattr(tool, "tool_name", None) or getattr(tool, "name", None)
            if not name:
                continue
            register_tool_app(
                name,
                resource_uri=uri,
                html=html,
                mcp_server=server_name,
                csp=csp,
            )
            registered += 1
        return registered

    return 0


def _resource_text_and_meta(contents: Any) -> tuple[str | None, dict[str, Any]]:
    items = contents if isinstance(contents, list) else [contents]
    for item in items:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        meta = getattr(item, "meta", None)
        if meta is None and isinstance(item, dict):
            meta = item.get("meta")
        mime = getattr(item, "mimeType", None) or getattr(item, "mime_type", None)
        if mime is None and isinstance(item, dict):
            mime = item.get("mimeType") or item.get("mime_type")
        csp = None
        if isinstance(meta, dict):
            ui = meta.get("ui")
            if isinstance(ui, dict):
                csp = ui.get("csp")
        return text, {"csp": csp or {}, "mimeType": mime}
    return None, {}


async def cache_apps_from_adapter(
    adapter: Any,
    tools: list[Any],
    *,
    fallback_server_name: str | None = None,
) -> int:
    """Read ui:// resources for tools that declare MCP Apps metadata."""
    registered = 0
    client = getattr(adapter, "_client", None)
    if client is None or not hasattr(client, "read_resource"):
        logger.warning("MCP adapter has no resource client; skipping Apps cache")
        return 0

    for tool in tools:
        metadata = getattr(tool, "metadata", None) or {}
        uri = extract_ui_resource_uri(metadata)
        if not uri:
            continue
        if fallback_server_name in HOST_FETCHED_SERVERS:
            # The web host reads this resource itself; no need to pull the HTML on every
            # agent start. Only the tool -> resource mapping is needed here.
            register_tool_app(
                tool.name,
                resource_uri=uri,
                mcp_server=fallback_server_name,
            )
            registered += 1
            continue
        try:
            contents = await client.read_resource(uri)
            html, extra = _resource_text_and_meta(contents)
            if not html:
                logger.warning("Empty UI resource for %s (%s)", tool.name, uri)
                continue
            register_tool_app(
                tool.name,
                resource_uri=uri,
                html=html,
                # The host proxy routes by the MCP *config key* ("canva", "notion-remote",
                # "weather"). The server's self-declared name ("Canva MCP Server") is only
                # a fallback: it is not unique or stable and the proxy would not know it.
                mcp_server=fallback_server_name or extract_mcp_server_name(metadata),
                csp=extra.get("csp"),
                mime_type=extra.get("mimeType"),
            )
            registered += 1
        except Exception as exc:
            logger.error("Failed to cache MCP App UI for %s: %s", tool.name, exc)
    return registered


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
            elif isinstance(block, dict):
                # Bedrock/Strands blocks are often {"text": "..."} without type=text.
                text = block.get("text")
                if isinstance(text, str) and block.get("type", "text") == "text":
                    texts.append(text)
                else:
                    import json

                    try:
                        texts.append(json.dumps(block, ensure_ascii=False))
                    except Exception:
                        texts.append(str(block))
            elif hasattr(block, "text"):
                texts.append(str(getattr(block, "text") or ""))
            else:
                texts.append(str(block))
        return "\n".join(t for t in texts if t)
    return str(content)
