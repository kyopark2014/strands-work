export interface Task {
  id: string;
  user_id: string;
  title: string;
  runtime_session_id: string;
  model_name: string;
  skills: string[];
  mcp_servers: string[];
  strands_tools: string[];
  guardrail_enabled: boolean;
  memory_enabled: boolean;
  pinned: boolean;
  created_at: string;
  updated_at: string;
}

export interface ToolEvent {
  type: "text" | "tool" | "tool_result" | "info";
  tool?: string;
  /** MCP server name when the tool belongs to a selected MCP server. */
  mcpServer?: string;
  /** Skill name when the tool is get_skill_instructions. */
  skillName?: string;
  input?: unknown;
  toolUseId?: string;
  data?: string;
}

export interface Message {
  id: string;
  task_id: string;
  role: "user" | "assistant";
  content: string;
  images: string[];
  tool_events: ToolEvent[];
  created_at: string;
}

export interface AppConfig {
  projectName: string;
  /** Present only after authentication. */
  skills?: string[];
  mcp_servers?: string[];
  strands_tools?: string[];
  models?: string[];
  default_model?: string;
  default_skills?: string[];
  default_mcp_servers?: string[];
  default_strands_tools?: string[];
}

/** True when /api/config returned authenticated capability catalogs. */
export function hasAuthenticatedConfig(
  config: AppConfig | null | undefined,
): config is AppConfig & {
  skills: string[];
  mcp_servers: string[];
  strands_tools: string[];
  models: string[];
  default_model: string;
} {
  return Boolean(
    config &&
      Array.isArray(config.skills) &&
      Array.isArray(config.mcp_servers) &&
      Array.isArray(config.strands_tools) &&
      Array.isArray(config.models) &&
      typeof config.default_model === "string",
  );
}

export interface StreamEvent {
  type: "token" | "text" | "tool" | "tool_result" | "info" | "done" | "error";
  data?: string;
  content?: string;
  images?: string[];
  tool_events?: ToolEvent[];
  tool?: string;
  mcpServer?: string;
  skillName?: string;
  input?: unknown;
  toolUseId?: string;
}

export type TaskRunStatus =
  | "idle"
  | "running"
  | "pending"
  | "done"
  | "error"
  | "missing";

export interface TaskRun {
  task_id: string;
  status: TaskRunStatus;
  content: string;
  images: string[];
  error?: string | null;
  source?: "messages" | "registry" | null;
  hydrated: boolean;
}
