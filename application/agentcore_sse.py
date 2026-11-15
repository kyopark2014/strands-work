import json
import logging
import sys

try:
    from application.tool_result_parsers import (
        ToolResultParseError,
        _append_references_to_result,
        get_tool_info,
    )
except ImportError:
    from tool_result_parsers import (
        ToolResultParseError,
        _append_references_to_result,
        get_tool_info,
    )

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("agentcore_sse")

# Cap tool payloads for logs / UI notifications (AgentCore already truncates
# stream payloads; this is a second guard for ECS-side flood).
_LOG_TRUNCATE_CHARS = 2_000
_NOTIFY_TRUNCATE_CHARS = 8_000


def _truncate_text(text: object, max_chars: int) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except TypeError:
            text = str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    suffix = f"\n...[truncated {omitted} chars]"
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def add_notification(notification_queue, message):
    if notification_queue is not None:
        notification_queue.notify(message)


def update_streaming_result(notification_queue, message):
    if notification_queue is not None:
        notification_queue.stream(message)


def tool_slot_update(
    notification_queue,
    slot_key: str,
    message: str,
    *,
    mcp_server: str | None = None,
    skill_name: str | None = None,
    mcp_app: dict | None = None,
):
    if notification_queue is not None:
        notification_queue.tool_update(
            slot_key,
            message,
            mcp_server=mcp_server,
            skill_name=skill_name,
            mcp_app=mcp_app,
        )


tool_info_list = dict()
tool_result_list = dict()
tool_name_list = dict()


def normalize_bedrock_message_content(content):
    """
    LangChain/Bedrock/Claude가 반환하는 message.content를 화면용 문자열로 만든다.
    - str: 그대로
    - list[dict]: Anthropic content blocks (type text, tool_use 등)에서 텍스트만 이어붙임
    - dict: 단일 블록이면 text 키 사용
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text" and "text" in content:
            return str(content["text"])
        if "text" in content:
            return str(content["text"])
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    continue
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def _collect_tool_result_artifacts(tool_name, tool_result, references: list, image_url: list) -> None:
    try:
        content, urls, refs = get_tool_info(tool_name, tool_result)
    except ToolResultParseError:
        logger.error(
            "Failed to parse tool result artifacts for %s",
            tool_name,
            exc_info=True,
        )
        return
    if refs:
        for reference in refs:
            references.append(reference)
        logger.info(f"refs: {refs}")
    if urls:
        for url in urls:
            image_url.append(url)
        logger.info(f"urls: {urls}")
    if content:
        logger.info(f"content: {content}")


def _process_strands_sse_event(data_json: dict, notification_queue, stream_state: dict) -> None:
    """Handle one strands SSE event and update stream_state in place."""
    if "data" in data_json:
        text = normalize_bedrock_message_content(data_json["data"])
        logger.info(f"[data] {_truncate_text(text, _LOG_TRUNCATE_CHARS)}")
        stream_state["current"] += text
        update_streaming_result(notification_queue, stream_state["current"])
        return

    if "result" in data_json:
        final_output = data_json["result"]
        logger.info(f"[result] {_truncate_text(final_output, _LOG_TRUNCATE_CHARS)}")
        if isinstance(final_output, dict):
            if final_output.get("cancelled"):
                stream_state["cancelled"] = True
            stream_state["result"] = final_output.get("messages", "")
            if "image_url" in final_output:
                stream_state["image_url"] = final_output.get("image_url", [])
                logger.info(f"image_url: {stream_state['image_url']}")
        else:
            stream_state["result"] = final_output
        logger.info(f"result: {stream_state['result']}")
        return

    # toolResult payloads also carry a "tool" label — check before "tool" input events
    if "toolResult" in data_json:
        tool_result = data_json["toolResult"]
        tool_use_id = data_json["toolUseId"]
        tool_name = tool_name_list.get(tool_use_id, data_json.get("tool", ""))
        logger.info(f"[tool_result] {_truncate_text(tool_result, _LOG_TRUNCATE_CHARS)}")
        tool_slot_update(
            notification_queue,
            f"{tool_use_id}:result",
            f"Tool Result: {_truncate_text(tool_result, _NOTIFY_TRUNCATE_CHARS)}",
            mcp_server=data_json.get("mcpServer"),
            skill_name=data_json.get("skillName"),
            mcp_app=data_json.get("mcpApp"),
        )
        _collect_tool_result_artifacts(
            tool_name,
            tool_result,
            stream_state["references"],
            stream_state["image_url"],
        )
        return

    if "tool" in data_json:
        tool = data_json["tool"]
        tool_input = data_json.get("input", "")
        tool_use_id = data_json["toolUseId"]
        tool_name_list[tool_use_id] = tool
        if tool_use_id not in tool_info_list:
            stream_state["current"] = ""
            tool_info_list[tool_use_id] = True
        tool_slot_update(
            notification_queue,
            f"{tool_use_id}:input",
            f"Tool: {tool}, Input: {_truncate_text(tool_input, _NOTIFY_TRUNCATE_CHARS)}",
            mcp_server=data_json.get("mcpServer"),
            skill_name=data_json.get("skillName"),
        )
        return


def _finalize_agent_result(result, current, references: list, notification_queue):
    empty = (
        result == ""
        or result == []
        or result is None
        or (isinstance(result, str) and not result.strip())
    )
    if empty and current:
        result = current

    if references:
        result = _append_references_to_result(result, references)

    if notification_queue is not None:
        final = result
        if not isinstance(final, str):
            final = (
                json.dumps(final, ensure_ascii=False)
                if isinstance(final, (list, dict))
                else str(final)
            )
        notification_queue.result(final)

    return result
