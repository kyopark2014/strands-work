import logging
import os
import sys
import asyncio

import chat
try:
    from application import run_cancel
except ImportError:
    import run_cancel
import httpx
import boto3
import utils
import strands_agent

from datetime import datetime, timezone
from urllib.parse import urlparse
from botocore.auth import SigV4Auth as BotocoreSigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("agent")

_original_httpx_async_init = httpx.AsyncClient.__init__

def _sigv4_region_for_bedrock_agentcore_url(url: str) -> str:
    host = urlparse(url).netloc
    parts = host.split(".")
    try:
        idx = parts.index("bedrock-agentcore")
        if idx + 1 < len(parts) and parts[idx + 1] != "amazonaws":
            return parts[idx + 1]
    except ValueError:
        pass
    return utils.load_config().get("region", "us-west-2")

def _patched_httpx_async_init(self, *args, **kwargs):
    async def sign_request(request: httpx.Request) -> None:
        url_str = str(request.url)
        if "bedrock-agentcore" not in url_str:
            return
        if ".gateway.bedrock-agentcore." in url_str:
            return
        if request.headers.get("Authorization"):
            return

        boto_session = boto3.Session()
        credentials = boto_session.get_credentials().get_frozen_credentials()

        parsed_url = urlparse(url_str)
        host = parsed_url.netloc
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        body = None
        if request.content:
            if isinstance(request.content, bytes):
                body = request.content
            else:
                try:
                    body = await request.aread()
                    if hasattr(request, "_content"):
                        request._content = body
                except Exception:
                    pass

        aws_headers = {
            "host": host,
            "x-amz-date": timestamp,
            "Content-Type": request.headers.get("Content-Type", "application/json"),
            "Accept": request.headers.get("Accept", "application/json, text/event-stream"),
        }
        if body:
            aws_headers["Content-Length"] = str(len(body))

        aws_request = AWSRequest(
            method=request.method,
            url=url_str,
            headers=aws_headers,
            data=body,
        )

        region = _sigv4_region_for_bedrock_agentcore_url(url_str)
        auth = BotocoreSigV4Auth(credentials, "bedrock-agentcore", region)
        auth.add_auth(aws_request)

        request.headers["X-Amz-Date"] = timestamp
        request.headers["Authorization"] = aws_request.headers["Authorization"]
        if credentials.token:
            request.headers["X-Amz-Security-Token"] = credentials.token

    if "event_hooks" not in kwargs:
        kwargs["event_hooks"] = {"request": [], "response": []}
    elif not isinstance(kwargs["event_hooks"], dict):
        kwargs["event_hooks"] = {"request": [], "response": []}
    if "request" not in kwargs["event_hooks"]:
        kwargs["event_hooks"]["request"] = []
    kwargs["event_hooks"]["request"].append(sign_request)

    _original_httpx_async_init(self, *args, **kwargs)


auth_type = "iam"
app = BedrockAgentCoreApp()


@app.entrypoint
async def agent_strands(payload):
    """Entry point: invoke the Strands agent with a payload.

    Thin wrapper; the actual orchestration lives in _run_agent_strands so the
    entrypoint itself stays free of business logic.
    """
    async for event in _run_agent_strands(payload):
        yield event


async def _run_agent_strands(payload):
    """Run one Strands agent turn: configure, stream the agent, and yield events."""
    logger.info(f"payload: {payload}")

    query = payload.get("prompt")
    mcp_servers = payload.get("mcp_servers", [])
    skill_list = payload.get("skill_list", [])
    strands_tools = payload.get("strands_tools", strands_agent.strands_tools or [])
    model_name = payload.get("model_name")
    user_id = payload.get("user_id")
    runtime_session_id = payload.get("runtime_session_id")

    files = payload.get("files") or []
    if not isinstance(files, list):
        files = [files] if files else []
    files = [str(f).strip() for f in files if str(f).strip()]

    logger.info(f"query: {query}")
    logger.info(f"files: {files}")
    logger.info(f"mcp_servers: {mcp_servers}")
    logger.info(f"skill_list: {skill_list}")
    logger.info(f"runtime_session_id (payload): {runtime_session_id}")
    logger.info(f"runtime_session_id (context): {strands_agent.get_runtime_session_id()}")
    cancel_id = runtime_session_id or strands_agent.get_runtime_session_id()
    if cancel_id:
        run_cancel.clear(cancel_id)

    task_id = (payload.get("task_id") or "").strip()
    if task_id:
        os.environ["TASK_ID"] = task_id
    else:
        os.environ.pop("TASK_ID", None)
    if runtime_session_id:
        os.environ["RUNTIME_SESSION_ID"] = str(runtime_session_id)
    else:
        os.environ.pop("RUNTIME_SESSION_ID", None)
    logger.info(f"task_id: {task_id or '(none)'}")

    cancelled = False

    skill_mode = payload.get("skill_mode")
    if skill_mode is None:
        skill_mode = "Enable" if skill_list else "Disable"

    if auth_type == "iam":
        httpx.AsyncClient.__init__ = _patched_httpx_async_init
        logger.info("Applied SigV4 monkey patch for Bedrock AgentCore MCP")

    chat.update(
        userId=user_id if user_id else chat.user_id,
        modelName=model_name if model_name else chat.model_name,
        debugMode=payload.get("debug_mode", chat.debug_mode),
        reasoningMode=payload.get("reasoning_mode", chat.reasoning_mode),
        skillMode=skill_mode,
        guardrailEnabled=payload.get("guardrail_enabled"),
        memoryEnabled=payload.get("memory_enabled"),
    )
    logger.info(f"guardrail_enabled: {chat.guardrail_enabled}")
    logger.info(f"memory_enabled: {chat.memory_enabled}")

    if query and chat.guardrail_enabled and not chat.uses_converse_guardrail():
        blocked, blocked_message = chat.check_input_guardrail(query)
        if blocked:
            yield {
                "result": {
                    "messages": [{"role": "assistant", "content": blocked_message}],
                    "image_url": [],
                }
            }
            return

    needs_agent = (
        strands_agent.selected_strands_tools != strands_tools
        or strands_agent.selected_mcp_servers != mcp_servers
        or strands_agent.selected_skill_list != skill_list
        or strands_agent.selected_skill_mode != skill_mode
        or strands_agent.selected_session_id != strands_agent.get_runtime_session_id()
        or strands_agent.selected_guardrail_enabled != chat.guardrail_enabled
        or strands_agent.selected_model_name != chat.model_name
        or strands_agent.selected_user_id != chat.user_id
        or strands_agent.agent is None
    )
    if needs_agent:
        strands_agent.selected_strands_tools = list(strands_tools)
        strands_agent.selected_mcp_servers = list(mcp_servers)
        strands_agent.selected_skill_list = list(skill_list)
        strands_agent.selected_skill_mode = skill_mode
        strands_agent.selected_session_id = strands_agent.get_runtime_session_id()
        strands_agent.selected_guardrail_enabled = chat.guardrail_enabled
        strands_agent.selected_model_name = chat.model_name
        strands_agent.selected_user_id = chat.user_id

        strands_agent.mcp_manager.stop_agent_clients()
        strands_agent.agent = strands_agent.create_agent(
            strands_tools, mcp_servers, skill_list
        )
        # create_agent starts persistent MCP once; list_tools reuses that stack.
    else:
        # Warm path: revive dead sessions or no-op when already running.
        strands_agent.mcp_manager.start_agent_clients(mcp_servers)

    strands_agent.maybe_sanitize_agent_history_for_model()

    message_content = query or ""
    if files:
        file_summaries = []
        for file_ref in files:
            file_name = chat._file_name_from_ref(file_ref)
            logger.info(f"analyzing uploaded file: {file_ref}")
            try:
                summary = chat.get_summary_of_uploaded_file(file_ref, prompt=query or "")
            except Exception as e:
                logger.error(
                    "Failed to summarize file %s: %s",
                    file_ref,
                    type(e).__name__,
                    exc_info=True,
                )
                summary = "파일 분석 중 오류가 발생했습니다"
            file_summaries.append(
                f"선택한 파일({file_name})의 내용을 요약하면 아래와 같습니다.\n"
                f"경로: {file_ref}\n\n{summary}"
            )
        message_content = (message_content + "\n\n" if message_content else "") + "\n\n".join(
            file_summaries
        )
        query = message_content
        logger.info(f"query with file summaries length: {len(query)}")

    final_output: dict = {"messages": "", "image_url": []}
    streamed_text = ""
    image_urls: list = []
    tool_names: dict[str, str] = {}
    tool_inputs: dict[str, object] = {}
    stop_reason: str | None = None

    with strands_agent.mcp_manager.get_active_clients(mcp_servers) as _:
        try:
            agent_stream = strands_agent.agent.stream_async(query)

            async for event in agent_stream:
                if cancel_id and run_cancel.is_cancelled(cancel_id):
                    cancelled = True
                    logger.info(
                        "Cancel detected for session %s; stopping strands stream",
                        cancel_id,
                    )
                    break
                if "data" in event:
                    text = event["data"]
                    streamed_text += text
                    logger.info(f"[data] {utils.truncate_for_log(text)}")
                    yield {"data": text}

                elif "result" in event:
                    final = event["result"]
                    stop_reason = getattr(final, "stop_reason", None)
                    if stop_reason:
                        logger.info(f"[stop_reason] {stop_reason}")
                    message = final.message
                    if message:
                        content = message.get("content", [])
                        text = content[0].get("text", "") if content else ""
                        logger.info(f"[result] {utils.truncate_for_log(text)}")
                        final_output = {"messages": text, "image_url": image_urls}

                    try:
                        import cloudwatch_metrics

                        strands_agent._log_prompt_cache_usage(final)

                        usage = cloudwatch_metrics.extract_token_usage(final)
                        if not usage:
                            metrics = getattr(final, "metrics", None)
                            logger.warning(
                                "Token usage missing on AgentResult; CloudWatch metrics skipped "
                                "(model=%s accumulated_usage=%s)",
                                chat.model_id,
                                getattr(metrics, "accumulated_usage", None) if metrics else None,
                            )
                        else:
                            cloudwatch_metrics.publish_token_metrics(chat.model_id, final)
                    except Exception as metric_err:
                        logger.warning(f"CloudWatch token metrics publish skipped: {metric_err}")

                elif "current_tool_use" in event:
                    current_tool_use = event["current_tool_use"]
                    name = current_tool_use.get("name", "")
                    input_val = current_tool_use.get("input", "")
                    tool_use_id = current_tool_use.get("toolUseId", "")
                    logger.info(
                        f"[current_tool_use] name={name}, "
                        f"input={utils.truncate_for_log(input_val)}"
                    )

                    if tool_use_id:
                        tool_names[tool_use_id] = name
                        if isinstance(input_val, dict):
                            tool_inputs[tool_use_id] = input_val
                    payload = {
                        "tool": name,
                        "input": input_val,
                        "toolUseId": tool_use_id,
                    }
                    payload.update(strands_agent.tool_label_fields(name, input_val))
                    yield payload

                elif "message" in event:
                    message = event["message"]
                    logger.info(f"[message] {utils.truncate_for_log(message)}")

                    msg_content = message.get("content", [])
                    for item in msg_content:
                        if "toolResult" not in item:
                            continue
                        tool_result = item["toolResult"]
                        tool_use_id = tool_result["toolUseId"]
                        tool_content = tool_result["content"]
                        tool_result_text = tool_content[0].get("text", "") if tool_content else ""
                        tool_name = tool_names.get(tool_use_id, "")
                        # Full text stays in the model transcript; truncate for logs/SSE
                        # so huge HTML (e.g. get_raw_text) cannot block stdout or flood UI.
                        logger.info(
                            f"[toolResult] {utils.truncate_for_log(tool_result_text)}, "
                            f"[toolUseId] {tool_use_id}, len={len(tool_result_text)}"
                        )

                        tool_result_payload = {
                            "toolResult": utils.truncate_for_stream(tool_result_text),
                            "toolUseId": tool_use_id,
                            "tool": tool_name,
                        }
                        tool_result_payload.update(
                            strands_agent.tool_label_fields(
                                tool_name, tool_inputs.get(tool_use_id)
                            )
                        )
                        yield tool_result_payload

                        _, urls, _ = chat.get_tool_info(tool_name, tool_result_text)
                        if urls:
                            for url in urls:
                                if url not in image_urls:
                                    image_urls.append(url)

                elif "contentBlockDelta" or "contentBlockStop" or "messageStop" or "metadata" in event:
                    pass
                else:
                    logger.info(f"event: {event}")

            if not cancelled and cancel_id and run_cancel.is_cancelled(cancel_id):
                cancelled = True

            if cancelled:
                logger.info(
                    "Run cancelled for session %s; partial result len=%s",
                    cancel_id,
                    len(streamed_text),
                )

            result_text = final_output.get("messages") or streamed_text

            if not (result_text or "").strip() and streamed_text.strip():
                result_text = streamed_text

            # Final stop_reason wins even when earlier turns left preamble text
            # (e.g. "확인해보겠습니다" + tools, then empty refusal).
            skip_memory = False
            if cancelled:
                skip_memory = True
            elif stop_reason == "content_filtered":
                result_text = (
                    "요청이 모델 안전 정책에 의해 차단되었습니다. "
                    "다른 모델로 시도하거나 질문을 바꿔 주세요."
                )
                skip_memory = True
            elif stop_reason == "guardrail_intervened":
                result_text = (
                    "요청이 Guardrail 안전 정책에 의해 차단되었습니다. "
                    "질문을 바꿔 주세요."
                )
                skip_memory = True
            elif stop_reason == "refusal":
                result_text = (
                    "모델이 이 요청에 대한 응답을 거부했습니다. "
                    "다른 모델로 시도하거나 질문을 바꿔 주세요."
                )
                skip_memory = True
            elif not (result_text or "").strip():
                result_text = "답변을 찾지 못하였습니다."

            final_output = {
                "messages": result_text,
                "image_url": image_urls,
            }

            if chat.memory_enabled and not skip_memory:
                chat.save_to_memory(query, final_output["messages"])
        except (GeneratorExit, asyncio.CancelledError, BrokenPipeError, ConnectionError) as exc:
            logger.info(
                "Agent stream interrupted (%s); treating as cancel partial=%s",
                type(exc).__name__,
                len(streamed_text),
            )
            final_output = {
                "messages": streamed_text or "",
                "image_url": image_urls,
                "cancelled": True,
            }
        except Exception:
            logger.exception("Agent stream_async failed")
            # If the client already stopped, do not surface a fake failure answer.
            if cancelled or (cancel_id and run_cancel.is_cancelled(cancel_id)):
                final_output = {
                    "messages": streamed_text or "",
                    "image_url": image_urls,
                    "cancelled": True,
                }
            else:
                final_output = {
                    "messages": "에이전트 응답 처리 중 오류가 발생했습니다.",
                    "image_url": image_urls,
                }

    yield {"result": final_output}


if __name__ == "__main__":
    app.run()
