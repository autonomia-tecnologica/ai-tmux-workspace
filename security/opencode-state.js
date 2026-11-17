"use strict";
/**
 * OpenCode V2 state plugin: records the real session id of a tab and reports the agent state to
 * `src/hook.py agent opencode`.
 *
 * The plugin holds no capability rule and blocks nothing. It is a native V2 plugin (a default export
 * with `id` and `setup(ctx)`, `ctx.event.subscribe`), copied to a directory under `$WS_STATE/settings/`
 * by `OpenCodeAdapter.prepare` and loaded through the generated config's `plugins` list. The workspace
 * launches OpenCode with `--standalone`, so this process inherits the tab's environment
 * (`WS_SESSION_TAB`, `WS_SESSION_ROOT`, `WS_DIR`, `WS_STATE`) from the terminal that started it.
 *
 * Session id contract: `recordSession` writes whatever OpenCode reports into `tabs/session-<tab>`
 * (`WS_STATE`/`WS_SESSION_TAB`), and the workspace reads it back for the next restore
 * (`session_state.session_for`). A tab with no recorded id therefore opens a fresh session, and
 * `workspace restart` regenerates this file so the restarted server loads this copy.
 *
 * Only the root session moves the tab: subagent (child) sessions emit their own execution and tool
 * events, and reporting those would flip the dot of a tab that is still waiting on the main turn.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** Events that change the dot. Everything else (text deltas, usage, titles) is ignored unread. */
const STATE_EVENTS = new Set([
  "session.created",
  "session.execution.started",
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
  "session.idle",
  "session.step.started",
  "session.tool.input.started",
  "session.tool.called",
  "session.tool.progress",
  "session.tool.success",
  "session.tool.failed",
  "session.text.started",
  "session.reasoning.started",
]);

/** A permission or question request concerns the whole tab, even when the asking session is a subagent. */
const ASK_EVENTS = new Set(["permission.asked", "permission.replied", "form.created",
                            "form.replied", "form.cancelled"]);

export function stateDir(env) {
  env = env || process.env;
  if (env.WS_STATE) return env.WS_STATE;
  // Same owner as src/paths.py: the control plane holds the state folder.
  const configDir = env.AT_WS_CONFIG
    ? path.dirname(path.resolve(env.AT_WS_CONFIG))
    : path.join(env.HOME || os.homedir(), ".config", "ai-tmux-workspace");
  return path.join(configDir, "state");
}

export function recordSession(input, env) {
  env = env || process.env;
  const tab = env.WS_SESSION_TAB;
  const sid = input && input.sessionID;
  if (!tab || !sid) return;
  const dir = path.join(stateDir(env), "tabs");
  const file = path.join(dir, "session-" + tab);
  const text = String(sid);
  try {
    fs.mkdirSync(dir, { recursive: true });
    let current = "";
    try {
      current = fs.readFileSync(file, "utf8");
    } catch (_error) {
      current = "";
    }
    if (current !== text) fs.writeFileSync(file, text);
  } catch (_error) {
    // the session id is a convenience; never fail the plugin because of it
  }
}

export function hookPath(env) {
  env = env || process.env;
  if (env.WS_DIR) return path.join(env.WS_DIR, "src", "hook.py");
  // The plugin copy lives in $WS_STATE/settings/opencode-plugin-<profile>/; the installed layout
  // puts the code under <layout>/app/src/hook.py.
  if (env.AT_WS_INSTALL_DIR) {
    const root = path.basename(env.AT_WS_INSTALL_DIR) === "app"
      ? path.dirname(env.AT_WS_INSTALL_DIR) : env.AT_WS_INSTALL_DIR;
    return path.join(root, "app", "src", "hook.py");
  }
  if (path.basename(HERE) === "security") return path.resolve(HERE, "..", "src", "hook.py");
  return path.join(os.homedir(), ".local", "share", "ai-tmux-workspace", "app", "src", "hook.py");
}

/**
 * Reports one OpenCode event to the workspace tab state (`src/hook.py agent opencode`). The Python
 * adapter (`src/agents/opencode.py`, `state_of`) translates the event name into the state; this file
 * only forwards what OpenCode said. The pid is this process, the agent itself: when it dies the tab
 * reads as `unknown`. Without the launcher's tab and root nothing is reported.
 */
export function reportState(event, options) {
  options = options || {};
  const env = options.env || process.env;
  if (!env.WS_SESSION_TAB || !env.WS_SESSION_ROOT) return;
  const python = options.python || env.WS_PYTHON || "python3";
  spawnSync(python, [hookPath(env), "agent", "opencode", env.WS_SESSION_TAB, String(process.pid)], {
    input: JSON.stringify(event || {}),
    encoding: "utf8",
    env,
    timeout: options.timeout || 5000,
    maxBuffer: 64 * 1024,
  });
}

/** OpenCode bus event reduced to what the adapter reads: its type and the session status type. */
export function stateEvent(event) {
  const data = (event && event.data) || {};
  const status = data.status && typeof data.status === "object" ? data.status.type : data.status;
  return { event: event && event.type, status: typeof status === "string" ? status : "" };
}

/** Session id carried by an event, or "" when the event has none. */
export function sessionID(event) {
  const data = (event && event.data) || {};
  return typeof data.sessionID === "string" ? data.sessionID : "";
}

function wanted(event) {
  const kind = event && event.type;
  return STATE_EVENTS.has(kind) || ASK_EVENTS.has(kind);
}

function watch(ctx) {
  const controller = new AbortController();
  let root = "";
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
        if (!wanted(event)) continue;
        if (ASK_EVENTS.has(event.type)) {
          reportState(stateEvent(event));
          continue;
        }
        const id = sessionID(event);
        if (!root) {
          // A child session is created with a parent: the first parentless session is the tab's own.
          if (event.type === "session.created" && event.data && event.data.parentID) continue;
          if (!id) continue;
          root = id;
          recordSession({ sessionID: id });
        }
        if (id !== root) continue;
        recordSession({ sessionID: id });
        reportState(stateEvent(event));
      }
    } catch (_error) {
      // the subscription ends with the server; nothing to report
    }
  })();
  return () => controller.abort();
}

export default {
  id: "ai-tmux-workspace.state",
  setup(ctx) {
    return watch(ctx);
  },
};
