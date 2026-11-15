import type { ToolEvent } from "../types";
import { McpAppFrame } from "./McpAppFrame";

interface Props {
  event: ToolEvent;
}

function formatToolInput(input: unknown): string {
  if (input === undefined || input === null) {
    return "(매개변수 없음)";
  }
  if (typeof input === "object" && !Array.isArray(input)) {
    const keys = Object.keys(input as Record<string, unknown>);
    if (keys.length === 0) {
      return "(매개변수 없음 — 기본값 사용)";
    }
  }
  return JSON.stringify(input, null, 2);
}

/** Make tool result / info payloads readable across multiple lines. */
function formatToolPayload(data: string | undefined): string {
  if (!data) return "";

  try {
    const parsed = JSON.parse(data) as unknown;
    if (Array.isArray(parsed)) {
      const texts = parsed
        .filter(
          (block): block is { type: string; text: string } =>
            !!block &&
            typeof block === "object" &&
            (block as { type?: unknown }).type === "text" &&
            typeof (block as { text?: unknown }).text === "string",
        )
        .map((block) => block.text);
      if (texts.length > 0) {
        return texts.join("\n\n");
      }
    }
    return JSON.stringify(parsed, null, 2);
  } catch {
    // Python-style repr often embeds literal \n / \t escape sequences.
    return data.replace(/\\n/g, "\n").replace(/\\t/g, "\t");
  }
}

/** Prefer explicit skillName, then fall back to skills / get_skill_instructions input. */
function skillNameFromEvent(event: ToolEvent): string | undefined {
  if (event.skillName) return event.skillName;
  if (event.tool !== "skills" && event.tool !== "get_skill_instructions") {
    return undefined;
  }
  const input = event.input;
  if (!input || typeof input !== "object" || Array.isArray(input)) return undefined;
  const skillName = (input as Record<string, unknown>)["skill_name"];
  if (typeof skillName === "string" && skillName.trim()) return skillName.trim();
  return undefined;
}

/** Prefer explicit mcpServer, then fall back to gateway-style `server___Tool` names. */
function mcpServerFromEvent(event: ToolEvent): string | undefined {
  if (event.mcpServer) return event.mcpServer;
  const tool = event.tool;
  if (!tool || !tool.includes("___")) return undefined;
  const prefix = tool.split("___")[0]?.trim();
  return prefix || undefined;
}

/** `Tools: {name}` or `Tools: {name} ({label})`. */
function formatToolLabel(
  tool?: string,
  mcpServer?: string,
  skillName?: string,
): string {
  if (!tool) return "Tools";
  if (mcpServer) return `Tools: ${tool} (${mcpServer})`;
  if (skillName) return `Tools: ${tool} (${skillName})`;
  return `Tools: ${tool}`;
}

function formatToolResultLabel(
  tool?: string,
  mcpServer?: string,
  skillName?: string,
): string {
  if (!tool) return "Tool result";
  if (mcpServer) return `Tool result: ${tool} (${mcpServer})`;
  if (skillName) return `Tool result: ${tool} (${skillName})`;
  return `Tool result: ${tool}`;
}

export function ToolCallCard({ event }: Props) {
  const skillName = skillNameFromEvent(event);
  const mcpServer = mcpServerFromEvent(event);
  if (event.type === "tool") {
    return (
      <details className="tool-card">
        <summary>{formatToolLabel(event.tool, mcpServer, skillName)}</summary>
        <pre>{formatToolInput(event.input)}</pre>
      </details>
    );
  }
  if (event.type === "tool_result") {
    const hasApp = Boolean(
      event.mcpApp && (event.mcpApp.html || (event.mcpApp.resourceUri && event.mcpApp.mcpServer)),
    );
    return (
      <div className="tool-card-stack">
        {hasApp && event.mcpApp ? <McpAppFrame app={event.mcpApp} /> : null}
        <details className="tool-card">
          <summary>
            {formatToolResultLabel(event.tool, mcpServer, skillName)}
          </summary>
          <pre>{formatToolPayload(event.data)}</pre>
        </details>
      </div>
    );
  }
  return (
    <details className="tool-card">
      <summary>Info</summary>
      <pre>{formatToolPayload(event.data)}</pre>
    </details>
  );
}
