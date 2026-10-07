"""Chat facade: owns module-level state and re-exports focused submodules."""
from tools.workspace import set_user_workspace

import utils
import info
import bedrock_data_retention
import boto3
import traceback
import uuid
import logging
import sys
import re
import os
import json

from botocore.config import Config
from langchain_aws import ChatBedrock
from langchain_aws import ChatBedrockConverse
from langchain_core.prompts import ChatPromptTemplate
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("chat")

os.environ["BYPASS_TOOL_CONSENT"] = "true"

workingDir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(workingDir, "config.json")

config = utils.load_config()

bedrock_region = config.get("region", "us-west-2")
projectName = config.get("projectName", "strands-runtime")
accountId = config.get("accountId", None)
knowledge_base_id = config.get("knowledge_base_id", None)
account_id = config.get("accountId", None)
user_id = "agent"

if accountId is None:
    try:
        sts = boto3.client("sts")
        accountId = sts.get_caller_identity()["Account"]
        config["accountId"] = accountId
    except Exception:
        logger.exception("Failed to resolve AWS account ID via STS")
        raise
account_id = accountId
region = config["region"] if "region" in config else "us-west-2"
logger.info(f"region: {region}")

s3_prefix = "docs"
s3_image_prefix = "images"
doc_prefix = s3_prefix + "/"
capture_prefix = "captures"

s3_bucket = config.get("s3_bucket")
path = config.get("sharing_url")
sharing_url = path

model_name = "Claude 4.6 Sonnet"
debug_mode = "Enable"
models = info.get_model_info(model_name)
model_id = models[0]["model_id"]
model_type = models[0]["model_type"]
bedrock_region = config.get("region", "us-west-2")
reasoning_mode = "Disable"
skill_mode = "Disable"
guardrail_enabled = True
memory_enabled = False

# Memory related variables
MSG_LENGTH = 100
map_chain = dict()
memory_chain = None
memory_id = None
actor_id = None
session_id = None

# RAG scratch state
number_of_results = 4
reference_docs = []
contentList = []

fileId = uuid.uuid4().hex

aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
aws_session_token = os.environ.get("AWS_SESSION_TOKEN")
aws_region = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")


def _module_model_id() -> str:
    """Return the module-level model_id without indexing globals()."""
    return model_id


def is_fable_model(model_id: str | None = None) -> bool:
    if not model_id:
        model_id = _module_model_id()
    return "fable" in model_id.lower()


def uses_adaptive_thinking(model_id: str | None = None) -> bool:
    if not model_id:
        model_id = _module_model_id()
    mid = model_id.lower()
    return (
        "fable" in mid
        or "claude-sonnet-5" in mid
        or "claude-5-sonnet" in mid
        or "claude-opus-5" in mid
        or "claude-5-opus" in mid
    )


# Model output token limits (from Anthropic / Bedrock model docs and quotas).
CLAUDE_5_MAX_OUTPUT_TOKENS = 128000  # Claude 5 Sonnet/Opus, Fable, Opus 4.6 API limit
CLAUDE_OPUS_4_5_MAX_OUTPUT_TOKENS = 64000  # Claude Opus 4.5 / Sonnet 4 / Haiku 4
CLAUDE_OPUS_4_MAX_OUTPUT_TOKENS = 32000  # Claude Opus 4
LEGACY_MODEL_MAX_OUTPUT_TOKENS = 8192  # Default for older / unspecified models

RESULT_TAG_OPEN = "<result>"
RESULT_TAG_CLOSE = "</result>"
RESULT_TAG_OPEN_LEN = len(RESULT_TAG_OPEN)
RESULT_TAG_CLOSE_LEN = len(RESULT_TAG_CLOSE)

# Reserve tokens for response formatting and safety margin when extended thinking is enabled.
REASONING_TOKEN_BUFFER = 1000


def get_max_output_tokens(model_id: str = "") -> int:
    """Return the max output tokens based on the model ID."""
    mid = (model_id or "").lower()
    if is_fable_model(model_id):
        return CLAUDE_5_MAX_OUTPUT_TOKENS
    if "claude-sonnet-5" in mid or "claude-5-sonnet" in mid:
        return CLAUDE_5_MAX_OUTPUT_TOKENS
    if "claude-opus-5" in mid or "claude-5-opus" in mid:
        return CLAUDE_5_MAX_OUTPUT_TOKENS
    if "claude-opus-4-6" in mid:
        return CLAUDE_5_MAX_OUTPUT_TOKENS
    if "claude-opus-4-5" in mid:
        return CLAUDE_OPUS_4_5_MAX_OUTPUT_TOKENS
    if "claude-opus-4" in mid or "claude-4-opus" in mid:
        return CLAUDE_OPUS_4_MAX_OUTPUT_TOKENS
    if "claude-sonnet-4" in mid or "claude-4-sonnet" in mid or "claude-haiku-4" in mid:
        return CLAUDE_OPUS_4_5_MAX_OUTPUT_TOKENS
    return LEGACY_MODEL_MAX_OUTPUT_TOKENS


def update(
    userId=None,
    modelName=None,
    debugMode=None,
    reasoningMode=None,
    skillMode=None,
    guardrailEnabled=None,
    memoryEnabled=None,
):
    global model_name, model_id, model_type, reasoning_mode, debug_mode, skill_mode
    global models, user_id, bedrock_region, guardrail_enabled, memory_enabled

    if userId is not None and userId != user_id:
        user_id = userId
        logger.info(f"user_id: {user_id}")
        initiate()
    if userId is not None:
        # Isolate artifacts + user skills under {SESSION_STORAGE_DIR}/{user_id}/
        # and ensure {user_id}/skills.list exists (create if missing).
        set_user_workspace(user_id)

    if modelName is not None and model_name != modelName:
        model_name = modelName
        logger.info(f"model_name: {model_name}")
        models = info.get_model_info(model_name)
        if models:
            model_id = models[0]["model_id"]
            model_type = models[0]["model_type"]
            bedrock_region = models[0]["bedrock_region"]

    if reasoningMode is not None and reasoningMode != reasoning_mode:
        reasoning_mode = reasoningMode
        logger.info(f"reasoning_mode: {reasoning_mode}")

    if debugMode is not None and debugMode != debug_mode:
        debug_mode = debugMode
        logger.info(f"debug_mode: {debug_mode}")

    if skillMode is not None and skillMode != skill_mode:
        skill_mode = skillMode
        logger.info(f"skill_mode: {skill_mode}")

    if guardrailEnabled is not None and guardrail_enabled != guardrailEnabled:
        guardrail_enabled = guardrailEnabled
        logger.info(f"guardrail_enabled: {guardrail_enabled}")

    if memoryEnabled is not None and memory_enabled != memoryEnabled:
        memory_enabled = memoryEnabled
        logger.info(f"memory_enabled: {memory_enabled}")


def _guardrail_config() -> dict | None:
    if not guardrail_enabled:
        return None
    runtime_config = config or utils.load_config()
    guardrail_id = runtime_config.get("guardrail_id")
    if not guardrail_id:
        return None
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": runtime_config.get("guardrail_version", "DRAFT"),
        "trace": "enabled",
    }


def uses_converse_guardrail() -> bool:
    return bool(_guardrail_config() and model_type in ("claude", "nova"))


def get_bedrock_model_guardrail_kwargs(model_type_value: str | None = None) -> dict:
    """Return BedrockModel guardrail kwargs for Strands SDK."""
    model_type_value = model_type_value or model_type
    guardrail_cfg = _guardrail_config()
    if not guardrail_cfg or model_type_value not in ("claude", "nova"):
        return {}
    return {
        "guardrail_id": guardrail_cfg["guardrailIdentifier"],
        "guardrail_version": guardrail_cfg["guardrailVersion"],
        "guardrail_trace": "enabled",
    }


def check_input_guardrail(text: str) -> tuple[bool, str]:
    """Return (blocked, message). When blocked, message is the guardrail response."""
    guardrail_cfg = _guardrail_config()
    if not guardrail_cfg or not text:
        return False, text

    try:
        client = boto3.client("bedrock-runtime", region_name=bedrock_region)
        response = client.apply_guardrail(
            guardrailIdentifier=guardrail_cfg["guardrailIdentifier"],
            guardrailVersion=guardrail_cfg["guardrailVersion"],
            source="INPUT",
            content=[{"text": {"text": text}}],
        )
        if response.get("action") == "GUARDRAIL_INTERVENED":
            logger.info("Guardrail blocked user input")
            for output in response.get("outputs", []):
                text_output = output.get("text", {})
                if text_output.get("text"):
                    return True, text_output["text"]
            return (
                True,
                "요청이 안전 정책에 의해 차단되었습니다. "
                "성적 표현 또는 프롬프트 공격이 감지되었습니다.",
            )
    except ClientError as e:
        logger.error(f"apply_guardrail failed: {e}")
    except Exception as e:
        logger.error(f"apply_guardrail failed: {e}")
    return False, text


def traslation(chat, text, input_language, output_language):
    system = (
        "You are a helpful assistant that translates {input_language} to {output_language} in <article> tags."
        "Put it in <result> tags."
    )
    human = "<article>{text}</article>"

    prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)])

    chain = prompt | chat
    try:
        result = chain.invoke(
            {
                "input_language": input_language,
                "output_language": output_language,
                "text": text,
            }
        )
        msg = result.content
    except Exception:
        err_msg = traceback.format_exc()
        logger.info(f"error message: {err_msg}")
        raise Exception("Not able to request to LLM")

    start = msg.find(RESULT_TAG_OPEN) + RESULT_TAG_OPEN_LEN
    end = len(msg) - RESULT_TAG_CLOSE_LEN
    return msg[start:end]


def isKorean(text):
    # check korean
    pattern_hangul = re.compile("[\u3131-\u3163\uac00-\ud7a3]+")
    word_kor = pattern_hangul.search(str(text))

    if word_kor and word_kor != "None":
        return True
    else:
        return False


def get_chat(extended_thinking):
    if model_type == "claude":
        maxOutputTokens = get_max_output_tokens(model_id)
    else:
        maxOutputTokens = 5120

    logger.info(
        f"LLM: bedrock_region: {bedrock_region}, modelId: {model_id}, model_type: {model_type}"
    )

    if "fable" in model_id.lower():
        bedrock_data_retention.ensure_fable_data_retention(
            model_id,
            bedrock_region=bedrock_region,
        )

    guardrail_cfg = _guardrail_config()
    if guardrail_cfg and model_type in ("claude", "nova"):
        if aws_access_key and aws_secret_key:
            boto3_bedrock = boto3.client(
                service_name="bedrock-runtime",
                region_name=bedrock_region,
                aws_access_key_id=aws_access_key,
                aws_secret_access_key=aws_secret_key,
                aws_session_token=aws_session_token,
                config=Config(
                    retries={"max_attempts": 30},
                    read_timeout=300,
                ),
            )
        else:
            boto3_bedrock = boto3.client(
                service_name="bedrock-runtime",
                region_name=bedrock_region,
                config=Config(
                    retries={"max_attempts": 30},
                    read_timeout=300,
                ),
            )
        converse_kwargs = {
            "model_id": model_id,
            "client": boto3_bedrock,
            "max_tokens": maxOutputTokens,

            "region_name": bedrock_region,
            "guardrail_config": guardrail_cfg,
        }
        if not uses_adaptive_thinking(model_id):
            converse_kwargs["temperature"] = 0.1
        if model_type == "claude":
            converse_kwargs["provider"] = "anthropic"
        converse_chat = ChatBedrockConverse(**converse_kwargs)
        converse_chat.streaming = False
        return converse_chat

    if model_type == "nova":
        STOP_SEQUENCE = '"\n\n<thinking>", "\n<thinking>", " <thinking>"'
    elif model_type == "claude":
        STOP_SEQUENCE = "\n\nHuman:"

    # Set AWS credentials
    aws_access_key_local = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_key_local = os.environ.get("AWS_SECRET_ACCESS_KEY")
    aws_session_token_local = os.environ.get("AWS_SESSION_TOKEN")

    # bedrock
    if aws_access_key_local and aws_secret_key_local:
        boto3_bedrock = boto3.client(
            service_name="bedrock-runtime",
            region_name=bedrock_region,
            aws_access_key_id=aws_access_key_local,
            aws_secret_access_key=aws_secret_key_local,
            aws_session_token=aws_session_token_local,
            config=Config(
                retries={"max_attempts": 30},
                read_timeout=300,
            ),
        )
    else:
        boto3_bedrock = boto3.client(
            service_name="bedrock-runtime",
            region_name=bedrock_region,
            config=Config(retries={"max_attempts": 30}),
        )
    if (
        model_type != "openai"
        and extended_thinking == "Enable"
        and not uses_adaptive_thinking(model_id)
    ):
        maxReasoningOutputTokens = 64000
        logger.info(f"extended_thinking: {extended_thinking}")
        thinking_budget = min(
            maxOutputTokens, maxReasoningOutputTokens - REASONING_TOKEN_BUFFER
        )

        parameters = {
            "max_tokens": maxReasoningOutputTokens,
            "thinking": {"type": "enabled", "budget_tokens": thinking_budget},
            "stop_sequences": [STOP_SEQUENCE],
        }
    elif model_type != "openai" and extended_thinking == "Disable":
        parameters = {
            "max_tokens": maxOutputTokens,
            "stop_sequences": [STOP_SEQUENCE],
        }
        if not uses_adaptive_thinking(model_id):
            parameters["temperature"] = 0.1
            parameters["top_k"] = 250
    elif model_type != "openai" and uses_adaptive_thinking(model_id):
        parameters = {
            "max_tokens": maxOutputTokens,
            "stop_sequences": [STOP_SEQUENCE],
        }
    else:
        parameters = {
            "max_tokens": maxOutputTokens,
            "stop_sequences": [STOP_SEQUENCE],
        }

    chat = ChatBedrock(  # new chat model
        model_id=model_id,
        client=boto3_bedrock,
        model_kwargs=parameters,
        region_name=bedrock_region,
    )

    return chat


def add_notification(notification_queue, message):
    if notification_queue is not None:
        notification_queue.notify(message)


def update_streaming_result(notification_queue, message):
    if notification_queue is not None:
        notification_queue.stream(message)


def update_tool_notification(notification_queue, tool_use_id, message):
    if notification_queue is not None:
        notification_queue.tool_update(tool_use_id, message)


def update_rag_result(notification_queue, message):
    if notification_queue is not None:
        notification_queue.stream(message)


####################### boto3 #######################
# General Conversation
#########################################################
def general_conversation(query):
    """Entry point: stream a general (non-agentic) conversation turn.

    Thin wrapper; the actual conversation orchestration lives in
    _run_general_conversation so the entry point stays free of business logic.
    """
    return _run_general_conversation(query)


def _run_general_conversation(query):
    global memory_chain

    if memory_chain is None:
        initiate()  # Initialize memory_chain

    if "fable" in model_id.lower():
        bedrock_data_retention.ensure_fable_data_retention(
            model_id,
            bedrock_region=bedrock_region,
        )

    system_prompt = (
        "당신의 이름은 서연이고, 질문에 대해 친절하게 답변하는 사려깊은 인공지능 도우미입니다."
        "상황에 맞는 구체적인 세부 정보를 충분히 제공합니다."
        "모르는 질문을 받으면 솔직히 모른다고 말합니다."
    )

    bedrock_client = boto3.client(
        service_name="bedrock-runtime",
        region_name=bedrock_region,
        config=Config(retries={"max_attempts": 30}),
    )

    # Process conversation history
    messages = []
    if memory_chain and hasattr(memory_chain, "load_memory_variables"):
        history = memory_chain.load_memory_variables({})["chat_history"]
        # Convert langchain messages to boto3 format
        for msg in history:
            if hasattr(msg, "content"):
                if msg.__class__.__name__ == "HumanMessage":
                    messages.append({"role": "user", "content": msg.content})
                elif msg.__class__.__name__ == "AIMessage":
                    messages.append({"role": "assistant", "content": msg.content})
        # Bedrock Converse API requirement: first message must be from user
        if messages and messages[0]["role"] == "assistant":
            messages = messages[1:]

    # Add current question
    messages.append({"role": "user", "content": f"Question: {query}"})

    # Set model parameters
    if model_type == "claude":
        maxOutputTokens = get_max_output_tokens(model_id)
        STOP_SEQUENCE = "\n\nHuman:"
    else:
        maxOutputTokens = 5120
        STOP_SEQUENCE = '"\n\n<thinking>", "\n<thinking>", " <thinking>"'

    if reasoning_mode == "Enable" and not uses_adaptive_thinking(model_id):
        maxReasoningOutputTokens = 64000
        thinking_budget = min(
            maxOutputTokens, maxReasoningOutputTokens - REASONING_TOKEN_BUFFER
        )
        parameters = {
            "max_tokens": maxReasoningOutputTokens,
            "temperature": 1,
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            },
            "stop_sequences": [STOP_SEQUENCE],
        }
    else:
        parameters = {
            "max_tokens": maxOutputTokens,
            "stop_sequences": [STOP_SEQUENCE],
        }
        if not is_fable_model(model_id) and not uses_adaptive_thinking(model_id):
            parameters["temperature"] = 0.1
            parameters["top_k"] = 250

    def stream_generator():
        try:
            if model_type == "claude":
                # Claude model format
                request_body = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": parameters["max_tokens"],
                    "stop_sequences": parameters.get("stop_sequences", []),
                    "system": system_prompt,
                    "messages": messages,
                }
                if "temperature" in parameters:
                    request_body["temperature"] = parameters["temperature"]
                if "top_k" in parameters:
                    request_body["top_k"] = parameters["top_k"]
                if "top_p" in parameters:
                    request_body["top_p"] = parameters["top_p"]

                if "thinking" in parameters:
                    request_body["thinking"] = parameters["thinking"]
            else:
                # Other model format
                request_body = {
                    "max_tokens": parameters["max_tokens"],
                    "system": system_prompt,
                    "messages": messages,
                }
                if "temperature" in parameters:
                    request_body["temperature"] = parameters["temperature"]

            # Call streaming response
            response = bedrock_client.invoke_model_with_response_stream(
                modelId=model_id, body=json.dumps(request_body)
            )

            full_content = ""
            for event in response["body"]:
                chunk = json.loads(event["chunk"]["bytes"].decode("utf-8"))

                if chunk.get("type") == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        full_content += text
                        yield text
                elif chunk.get("type") == "message_delta":
                    # Message complete
                    pass
                elif chunk.get("type") == "message_stop":
                    # Streaming ended
                    pass

            # Process <reasoning> tag
            if "<reasoning>" in full_content and "</reasoning>" in full_content:
                reasoning_start = full_content.find("<reasoning>") + 11
                reasoning_end = full_content.find("</reasoning>")
                reasoning_content = full_content[reasoning_start:reasoning_end]
                logger.info("reasoning_content: %s", reasoning_content)

            logger.info(f"full_content: {full_content}")

        except Exception:
            logger.exception("Not able to request to LLM")
            raise Exception("Unable to process request")

    return stream_generator()


# ---------------------------------------------------------------------------
# Re-exports from focused submodules (preserve `import chat; chat.X` API)
# ---------------------------------------------------------------------------
from chat_memory import (  # noqa: E402
    clear_chat_history,
    initiate,
    initiate_memory,
    save_chat_history,
    save_to_memory,
)
from chat_s3 import (  # noqa: E402
    create_object,
    get_s3_client,
    get_s3_resource,
    updata_object,
    upload_to_s3,
    upload_to_s3_artifacts,
)
from chat_documents import (  # noqa: E402
    MAX_IMAGE_PIXELS,
    _file_name_from_ref,
    _resize_and_encode,
    extract_text,
    get_summary,
    get_summary_of_uploaded_file,
    load_csv_document,
    load_document,
    summarize_image,
    summary_image,
)
from chat_tools import (  # noqa: E402
    _build_tool_reference,
    _extract_rag_references_from_payload,
    _format_references_markdown,
    _sanitize_reference_text,
    get_tool_info,
)
from chat_rag import (  # noqa: E402
    MAX_RETRIEVE_PAGES,
    _bedrock_retrieve_pages,
    retrieve,
    run_rag_using_retrieve_and_generate,
    run_rag_with_knowledge_base,
)
