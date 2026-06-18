import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import { spawn } from "child_process"
import path from "path"
import { fileURLToPath } from "url"

type PluginOptions = Record<string, unknown>

const PLUGIN_DIR = path.dirname(fileURLToPath(import.meta.url))
const DEFAULT_HOOK_CMD = path.join(PLUGIN_DIR, "scripts", "ripple-memory-mimocode-hook.cmd")

function stringOption(options: PluginOptions | undefined, key: string): string {
  const value = options?.[key]
  return typeof value === "string" ? value.trim() : ""
}

function resolveHookCmd(options?: PluginOptions): string {
  return (
    stringOption(options, "hookCommand") ||
    process.env.RIPPLE_MEMORY_MIMOCODE_HOOK_CMD ||
    DEFAULT_HOOK_CMD
  )
}

function lastJsonLine(stdout: string): Record<string, unknown> {
  const lines = stdout.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const parsed = JSON.parse(lines[index])
      if (typeof parsed === "object" && parsed !== null) return parsed
    } catch {
      // Keep scanning in case a debug line preceded the JSON payload.
    }
  }
  return {}
}

async function callHook(
  payload: Record<string, unknown>,
  options?: PluginOptions,
  timeoutMs = 3000,
): Promise<Record<string, unknown>> {
  const hookCmd = resolveHookCmd(options)
  return new Promise((resolve) => {
    let settled = false
    const finish = (value: Record<string, unknown>) => {
      if (settled) return
      settled = true
      resolve(value)
    }

    const proc = spawn("cmd.exe", ["/d", "/c", hookCmd], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    })
    const timer = setTimeout(() => {
      try {
        proc.kill()
      } catch {
        // Fail open if the host cannot stop the hook process.
      }
      finish({})
    }, timeoutMs)

    let stdout = ""
    proc.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString("utf8") })
    proc.stderr.on("data", () => {})

    proc.on("error", () => {
      clearTimeout(timer)
      finish({})
    })
    proc.on("close", () => {
      clearTimeout(timer)
      finish(lastJsonLine(stdout))
    })

    try {
      proc.stdin.write(JSON.stringify(payload))
      proc.stdin.end()
    } catch {
      clearTimeout(timer)
      finish({})
    }
  })
}

function extractTextParts(output: { message?: any; parts?: any[] }): string {
  const parts = Array.isArray(output.parts) ? output.parts : output.message?.parts
  if (!Array.isArray(parts)) return ""
  return parts
    .filter((part: any) => part?.type === "text" && typeof part.text === "string")
    .map((part: any) => part.text)
    .join("\n")
    .trim()
}

export default async function RippleMemoryPlugin(
  input: PluginInput,
  options?: PluginOptions,
): Promise<Hooks> {
  const directory = input.directory
  const project = input.project?.name || ""

  return {
    "chat.message": async (hookInput, output) => {
      const userText = extractTextParts(output)
      if (!userText) return

      const result = await callHook({
        hook_event_name: "user_prompt_submit",
        cwd: directory,
        project,
        session_id: hookInput.sessionID || "",
        user_text: userText,
      }, options)

      const context = result.context
      if (typeof context === "string" && context.trim()) {
        output.parts = output.parts || []
        output.parts.push({
          type: "text",
          text: `\n\n[Ripple Memory Context]\n${context}`,
        } as any)
      }
    },

    "experimental.chat.system.transform": async (hookInput, output) => {
      const result = await callHook({
        hook_event_name: "session_start",
        cwd: directory,
        project,
        session_id: hookInput.sessionID || "",
      }, options)

      const context = result.context
      if (typeof context === "string" && context.trim()) {
        output.system = output.system || []
        output.system.push(`[Ripple Memory Context]\n${context}`)
      }
    },

    "experimental.session.compacting": async (hookInput, output) => {
      const result = await callHook({
        hook_event_name: "session_start",
        cwd: directory,
        project,
        session_id: hookInput.sessionID || "",
      }, options)

      const context = result.context
      if (typeof context === "string" && context.trim()) {
        output.context = output.context || []
        output.context.push(`[Ripple Memory Latch]\n${context}`)
      }
    },
  }
}
