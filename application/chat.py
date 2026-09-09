"""Server-side chat backend for the Web UI ECS task.

Uses boto3 (bedrock-runtime, sts) with the task IAM role — not browser JavaScript.
Direct frontend→AWS SDK calls (AR1) do not apply to this module.
"""

import boto3
import logging
import sys
try:
    from application import info, utils, bedrock_data_retention
except ImportError:
    import info
    import utils
    import bedrock_data_retention

from langchain_aws import ChatBedrock, ChatBedrockConverse
from langchain_openai import ChatOpenAI
from botocore.config import Config

logging.basicConfig(
    level=logging.INFO,  # Default to INFO level
    format='%(filename)s:%(lineno)d | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stderr)
    ]
)
logger = logging.getLogger("chat")

MAX_REASONING_OUTPUT_TOKENS = 64000
REASONING_BUFFER_TOKENS = 1000

config = utils.load_config()
bedrock_region = config['region']
account_id = config['accountId']
project_name = config['projectName']

model_name = "Claude 4.5 Haiku"
model_type = "claude"
models = info.get_model_info(model_name)
model_id = models[0]["model_id"]

def update(model_name_param):
    global model_name, models, model_type, model_id

    if model_name_param is not model_name:
        model_name = model_name_param
        logger.info(f"modelName: {model_name_param}")

        models = info.get_model_info(model_name)
        model_type = models[0]["model_type"]
        model_id = models[0]["model_id"]
        logger.info(f"model_id: {model_id}")
        logger.info(f"model_type: {model_type}")

def _build_openai_chat(profile: dict, max_output_tokens: int):
    """Build OpenAI-on-Bedrock chat model (Mantle Responses API or Converse)."""
    bedrock_region = profile["bedrock_region"]
    model_id = profile["model_id"]
    mantle_api = profile.get("mantle_api", "chat")

    if mantle_api == "responses":
        def bearer_token_provider() -> str:
            return bedrock_data_retention.get_bedrock_bearer_token(bedrock_region)

        # bedrock-mantle is the Amazon Bedrock OpenAI-compatible endpoint (not a separate
        # AWS service): https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html
        return ChatOpenAI(
            model=model_id,
            api_key=bearer_token_provider,
            base_url=f"https://bedrock-mantle.{bedrock_region}.api.aws/openai/v1",
            use_responses_api=True,
            max_tokens=max_output_tokens,
        )

    # GPT-5.6 / Astra etc.: Bedrock Converse + inference profile (us.openai.*)
    converse_chat = ChatBedrockConverse(
        model=model_id,
        region_name=bedrock_region,
        max_tokens=max_output_tokens,
        provider="openai",
    )
    converse_chat.streaming = False
    return converse_chat

def get_chat(extended_thinking=None):
    # Set default value if not provided or invalid
    if extended_thinking is None or extended_thinking not in ['Enable', 'Disable']:
        extended_thinking = 'Disable'

    logger.info(f"model_name: {model_name}")
    profile = models[0]
    bedrock_region =  profile['bedrock_region']
    model_id = profile['model_id']
    model_type = profile['model_type']
    max_output_tokens = 4096 # 4k
    logger.info(f"LLM: bedrock_region: {bedrock_region}, modelId: {model_id}, model_type: {model_type}")

    if profile["model_type"] == "openai":
        return _build_openai_chat(profile, max_output_tokens)

    if profile['model_type'] == 'nova':
        STOP_SEQUENCE = '"\n\n<thinking>", "\n<thinking>", " <thinking>"'
    elif profile['model_type'] == 'claude':
        STOP_SEQUENCE = "\n\nHuman:"
    else:
        STOP_SEQUENCE = ""
                          
    # bedrock
    try:
        boto3_bedrock = boto3.client(
            service_name='bedrock-runtime',
            region_name=bedrock_region,
            config=Config(
                retries = {
                    'max_attempts': 30
                }
            )
        )
    except Exception:
        logger.exception("Failed to create bedrock-runtime client")
        raise
    
    if extended_thinking=='Enable':
        logger.info(f"extended_thinking: {extended_thinking}")
        thinking_budget = min(max_output_tokens, MAX_REASONING_OUTPUT_TOKENS - REASONING_BUFFER_TOKENS)

        parameters = {
            "max_tokens":MAX_REASONING_OUTPUT_TOKENS,
            "temperature":1,            
            "thinking": {
                "type": "enabled",
                "budget_tokens": thinking_budget
            },
            "stop_sequences": [STOP_SEQUENCE]
        }
    else:
        parameters = {
            "max_tokens":max_output_tokens,     
            "temperature":0.1,
            "top_k":250,
            "stop_sequences": [STOP_SEQUENCE]
        }

    chat = ChatBedrock(   # new chat model
        model_id=model_id,
        client=boto3_bedrock, 
        model_kwargs=parameters,
        region_name=bedrock_region
    )    
    return chat
