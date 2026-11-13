"use strict";
/**
 * OpenCode adapter for the central capability policy.
 *
 * This file holds no rule of its own: every decision comes from the policy engine CLI, the same
 * `policy.py check` the Claude Code hook uses (`src/policy.py`). The interpreter is `WS_PYTHON`
 * (exported by `workspace`) with a plain `python3` fallback, passed as an argv array and never
 * through a shell, so a Homebrew path or a path with spaces reaches the process unchanged.
 * `decide`/`decideMany` are exported so the test suite can run the same battery of actions through
 * both adapters and compare the decisions.
 *
 * OpenCode's `tool.execute.before` has no "ask" channel in the plugin API, so `ask` is surfaced as
 * a thrown error: the tool does not run unattended, which is the fail-closed reading of the same
 * decision. The engine still reports `ask`, and the decision comparison is on the engine result.
 *
 * Session id contract: this plugin is where a tab gets its id. `recordSession` writes whatever
 * OpenCode reports into `tabs/session-<tab>` (`WS_STATE`/`WS_SESSION_TAB`), and the workspace reads
 * it back for the next restore (`session_state.session_for`). A tab with no recorded id therefore
 * opens a fresh session: the workspace never asks OpenCode to continue the repo's last session for
 * a tab, and `workspace restart` regenerates this file so the restarted server loads this copy.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

function resolveCli(env) {
  env = env || process.env;
  if (env.WS_POLICY_CLI) return env.WS_POLICY_CLI;
  if (env.WS_DIR) return path.join(env.WS_DIR, "src", "policy.py");
  const local = path.join(__dirname, "policy.py");
  if (fs.existsSync(local)) return local;
  return path.resolve(__dirname, "..", "src", "policy.py");
}

function i18nPath(env) {
  env = env || process.env;
  if (env.WS_I18N) return env.WS_I18N;
  const candidates = [];
  if (env.WS_DIR) candidates.push(path.join(env.WS_DIR, "i18n", "pt-BR.json"));
  candidates.push(path.resolve(__dirname, "..", "i18n", "pt-BR.json"));
  candidates.push(path.join(__dirname, "i18n", "pt-BR.json"));
  return candidates.find((candidate) => fs.existsSync(candidate)) || "";
}

function translate(reason, capability, env) {
  const file = i18nPath(env);
  if (file) {
    try {
      const strings = JSON.parse(fs.readFileSync(file, "utf8"));
      const text = strings[reason];
      if (typeof text === "string") {
        return text.replace(/\{capability\}/g, capability || "-");
      }
    } catch (_error) {
      // fall through to the key
    }
  }
  return reason || "policy.reason.error";
}

function decide(event, options) {
  options = options || {};
  const env = options.env || process.env;
  const python = options.python || env.WS_PYTHON || "python3";
  const cli = options.cli || resolveCli(env);
  const result = spawnSync(python, [cli, "check"], {
    input: JSON.stringify(event || {}),
    encoding: "utf8",
    env,
    timeout: options.timeout || 5000,
    maxBuffer: 1024 * 1024,
  });
  if (result.error || result.status !== 0 || !result.stdout || !result.stdout.trim()) {
    return { decision: "deny", capability: null, reason: "policy.reason.error" };
  }
  try {
    return JSON.parse(result.stdout);
  } catch (_error) {
    return { decision: "deny", capability: null, reason: "policy.reason.error" };
  }
}

function decideMany(events, options) {
  return (events || []).map((event) => decide(event, options));
}

/**
 * ACP-shaped permission alias: the agent layer's `permission` asks the same central decision, so the
 * same battery of actions returns the same decision through the Claude hook and this adapter.
 */
function permission(event, options) {
  return decide(event, options);
}

function opencodeEvent(input, output) {
  const event = {
    tool_name: input && (input.tool || input.toolName || input.name),
    tool_input: (output && (output.args || output.tool_input || output.toolInput)) || {},
    session_id: input && (input.sessionID || input.sessionId || input.session_id),
  };
  const agent = input && (input.agentID || input.agentId || input.agent_id);
  if (agent) event.agent_id = agent;
  // `auto` is not fixed at launch: the engine reads the event's permission mode, like the Claude
  // hook does, so both adapters upgrade `ask` only inside the declared box.
  const mode = input && (input.permissionMode || input.permission_mode || input.mode);
  if (typeof mode === "string" && mode) event.permission_mode = mode;
  return event;
}

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
  const result = decide(opencodeEvent(input, output), options);
  // `privileged` (item 154) is not run from this sync path yet: it fails closed here, never as allow.
  if (result.decision === "deny" || result.decision === "ask" || result.decision === "privileged") {
    const env = (options && options.env) || process.env;
    const error = new Error(translate(result.reason, result.capability, env));
    error.policy = result;
    throw error;
  }
  return result;
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
module.exports.decide = decide;
module.exports.decideMany = decideMany;
module.exports.permission = permission;
module.exports.before = before;
module.exports.recordSession = recordSession;
module.exports.reportState = reportState;
module.exports.stateEvent = stateEvent;
module.exports.stateDir = stateDir;
module.exports.resolveCli = resolveCli;
module.exports.translate = translate;
module.exports.default = plugin;
