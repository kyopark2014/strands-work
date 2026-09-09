"""Assemble application UI config (models, skills, MCP, strands tools)."""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    from application import utils
except ImportError:
    import utils

logger = logging.getLogger("config_service")

_APPLICATION_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODELS = [
    "Claude 5.0 Sonnet",
    "Claude 5.0 Opus",
    "Claude 4.6 Sonnet",
    "Claude Fable 5",
    "Claude Fable 5.1",
    "Claude 4.8 Opus",
    "Claude 4.7 Opus",
    "Claude 4.6 Opus",
    "Claude 4.5 Opus",
    "Claude 4.5 Sonnet",
    "Claude 4.5 Haiku",
    "OpenAI GPT 5.4",
    "OpenAI GPT 5.5",
    "OpenAI GPT 6 Astra",
    "OpenAI GPT 5.6 Sol",
    "OpenAI GPT 5.6 Terra",
    "OpenAI GPT 5.6 Luna",
    "OpenAI OSS 120B",
    "OpenAI OSS 20B",
]

DEFAULT_MODEL = "Claude 4.6 Sonnet"
STRANDS_TOOLS = ["current_time", "file_read", "file_write", "http_request"]
DEFAULT_STRANDS_TOOLS = ["current_time", "file_read", "file_write"]


def load_capability_list_from_path(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
    except FileNotFoundError:
        logger.warning("Capability list not found: %s", path)
        return []


def load_capability_list(filename: str) -> list[str]:
    path = os.path.join(_APPLICATION_DIR, filename)
    return load_capability_list_from_path(path)


def get_public_config() -> dict[str, Any]:
    """Minimal fields required before login (login screen branding)."""
    config = utils.load_config()
    return {
        "projectName": config.get("projectName", "agent"),
    }


def get_application_config(user_id: str | None = None) -> dict[str, Any]:
    """Full UI capability catalogs. Call only for authenticated sessions.

    Skills are loaded from {SESSION_STORAGE_DIR}/{user_id}/skills.list on the
    shared S3 Files mount (same store as AgentCore ``/mnt/workspace``). Existing
    runtime-updated lists are preserved; missing lists are seeded from
    application/skills.list plus ``{user_id}/skills/``.
    """
    skills_path = utils.ensure_user_skills_list(user_id)
    skill_options = load_capability_list_from_path(skills_path)
    logger.info("Loaded skills from %s (%d)", skills_path, len(skill_options))
    mcp_options = load_capability_list("mcp.list")
    # Prefer per-user settings.json; fall back to favorite_tools.json.
    default_skills, default_mcp = utils.get_user_tool_defaults(user_id)
    config = utils.load_config()
    default_strands_tools = (
        config.get("default_strands_tool_selections") or DEFAULT_STRANDS_TOOLS
    )
    default_skills = [
        skill_name for skill_name in default_skills if skill_name in skill_options
    ]
    default_mcp = [
        mcp_name for mcp_name in default_mcp if mcp_name in mcp_options
    ]
    default_strands_tools = [
        tool_name for tool_name in default_strands_tools if tool_name in STRANDS_TOOLS
    ]
    if not default_skills and "skill-creator" in skill_options:
        default_skills = ["skill-creator"]
    if not default_mcp:
        logger.info("No initial MCP defaults matched current capability list")
    if not default_strands_tools:
        default_strands_tools = DEFAULT_STRANDS_TOOLS
    return {
        **get_public_config(),
        "skills": skill_options,
        "mcp_servers": mcp_options,
        "strands_tools": STRANDS_TOOLS,
        "models": MODELS,
        "default_model": DEFAULT_MODEL,
        "default_skills": default_skills,
        "default_mcp_servers": default_mcp,
        "default_strands_tools": default_strands_tools,
    }
