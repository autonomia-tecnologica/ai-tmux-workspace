"use strict";
/**
 * OpenCode adapter for the workspace tab state. It holds no capability rule and blocks nothing:
 * it records the real session id of a tab and reports OpenCode events to `src/hook.py agent opencode`.
 *
 * Session id contract: this plugin is where a tab gets its id. `recordSession` writes whatever
 * OpenCode reports into `tabs/session-<tab>` (`WS_STATE`/`WS_SESSION_TAB`), and the workspace reads
 * it back for the next restore (`session_state.session_for`). A tab with no recorded id therefore
 * opens a fresh session, and `workspace restart` regenerates this file so the restarted server loads
 * this copy.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

function stateDir(env) {
  env = env || process.env;
  if (env.WS_STATE) return env.WS_STATE;
  // Same owner as src/paths.py: the control plane holds the state folder.
  const configDir = env.AT_WS_CONFIG
    ? path.dirname(path.resolve(env.AT_WS_CONFIG))
    : path.join(env.HOME || os.homedir(), ".config", "ai-tmux-workspace");
  return path.join(configDir, "state");
}

function recordSession(input, env) {
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
    // the session id is a convenience; never fail the before hook because of it
  }
}

function hookPath(env) {
  env = env || process.env;
  if (env.WS_DIR) return path.join(env.WS_DIR, "src", "hook.py");
  return path.resolve(__dirname, "..", "src", "hook.py");
}

/**
 * Reports one OpenCode event to the workspace tab state (`src/hook.py agent opencode`). The Python
 * adapter (`src/agents/opencode.py`, `state_of`) translates the event name into the state; this file
 * only forwards what OpenCode said. The pid is this process, the agent itself: when it dies the tab
 * reads as `unknown`. Without the launcher's tab and root nothing is reported.
 */
function reportState(event, options) {
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
function stateEvent(event) {
  const properties = (event && event.properties) || {};
  const status = properties.status && typeof properties.status === "object"
    ? properties.status.type : properties.status;
  return { event: event && event.type, status: typeof status === "string" ? status : "" };
}

function before(input, output, options) {
  recordSession(input, (options && options.env) || process.env);
  reportState({ event: "tool.execute.before" }, options);
}

async function plugin(_context) {
  return {
    "tool.execute.before": async (input, output) => before(input, output),
    "tool.execute.after": async () => {
      reportState({ event: "tool.execute.after" });
    },
    event: async ({ event }) => {
      reportState(stateEvent(event));
    },
  };
}

module.exports = plugin;
module.exports.before = before;
module.exports.recordSession = recordSession;
module.exports.reportState = reportState;
module.exports.stateEvent = stateEvent;
module.exports.stateDir = stateDir;
module.exports.default = plugin;
