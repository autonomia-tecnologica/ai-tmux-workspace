# Threat model — workspace agent environment

Status: draft for review · Date: 2026-11-09 · Model version: 2026-11-09

The machine-readable source of truth is [`docs/threat-model.json`](threat-model.json).
This Markdown file is a rendered view; if the two disagree, the JSON wins.

## Scope

The ai-tmux-workspace agent environment: launcher, tmux layout, profiles (Claude Code and OpenCode), central capability policy, conference mode, persistence under ~/.config/ai-tmux-workspace, installed harness and installer/killswitch.

Out of scope is recorded per item in the JSON and summarized below.

## Frameworks

| Framework | Version | URL |
|---|---|---|
| OWASP Top 10 for LLM Applications | 2025 | https://genai.owasp.org/llm-top-10/ |
| OWASP Top 10 for Agentic Applications | 2025-12 (ASI01–ASI10) | https://genai.owasp.org/2025/12/09/owasp-genai-security-project-releases-top-10-risks-and-mitigations-for-agentic-ai-security/ |
| MITRE ATLAS | v2026.10 | https://github.com/mitre-atlas/atlas-data/releases/tag/v2026.10 |

OWASP supplies the requirement list. ATLAS supplies the adversarial-technique mapping,
taken from the official v2026.10 STIX data; IDs in this document were checked against that
data, not against a secondary crosswalk.

## Status summary

| Status | Count |
|---|---:|
| `out_of_scope` | 8 |
| `needs_decision` | 3 |
| `implemented` | 16 |
| `not_implemented` | 30 |
| **total** | **57** |

The only statuses are `out_of_scope`, `needs_decision`, `implemented` and
`not_implemented`. There is no `partial`: a partial control is `not_implemented` with a
precise failure, or it is split into smaller requirements.

## Scoping rules (2026-11-09 refinement)

1. A repo session handles only its own repo plus `/tmp`: code, `README.md` and
   `BACKLOG.md` of that repo. Outside the repo, only `/tmp` is allowed (R-SCOPE-01, D-001).
2. The main folder is the main repo from `config.yaml` (today `~/Codes/home`). It is not a
   development session: it may manage all configured repos' `README.md`/`BACKLOG.md` only,
   never their code (R-SCOPE-02, R-SCOPE-05, D-005).
3. Conference participants read only their own structural files (`SECURITY.md`, `DESIGN.md`,
   `ARCH.md`, `README.md`, `BACKLOG.md`), only when asked, and write only `README.md` (as a
   proposal) and `BACKLOG.md` (R-SCOPE-03).
4. No agent role writes the control plane (`~/.config/ai-tmux-workspace`); the workspace
   process and the installer do (R-SCOPE-04, R-POL-07).
5. Development is strongly recommended inside each repo's own folder, for individualized
   context (R-SCOPE-06).

The current `home` role (`repo.outside: allow`, `config.write: allow`) is broader than rules
2 and 4. That is a P0 gap, not an accepted design. D-005 fixes the owner of the main folder,
not the breadth of the role.

## Resolved decisions

| ID | Date | Decision | Requirement(s) |
|---|---|---|---|
| `D-001` | 2026-11-09 | The repo box is the repo plus /tmp. Outside the repo, only /tmp is allowed. | R-SCOPE-01 |
| `D-002` | 2026-11-09 | The conference log sent in each prompt has a hard cap with visible truncation. | R-CONF-07 |
| `D-003` | 2026-11-09 | When a secret is found in README.md/BACKLOG.md, block the run. | R-SEC-05 |
| `D-004` | 2026-11-09 | A README.md write requires showing the proposed diff and waiting for confirmation. | R-CONF-17 |
| `D-005` | 2026-11-09 | The main folder is the main repo from config.yaml (today ~/Codes/home). It manages all configured repos' README.md/BACKLOG.md only; the workspace control plane stays workspace-only. | R-SCOPE-02, R-SCOPE-05, R-HOME-02 |

## P0 — blocking

| ID | Requirement | Status | OWASP | ATLAS | Failure / reason |
|---|---|---|---|---|---|
| `R-SCOPE-01` | A repo session handles only its own repo plus /tmp | `not_implemented` | LLM06, ASI03 | AML.T0053, AML.T0085.001 | The capability table would deny repo.outside and allow /tmp, but the launcher never sets WS_SESSION_ROLE/WS_SESSION_ROOT, so the policy is inactive and the agent keeps whatever access the OS user has. |
| `R-SCOPE-02` | The main repo from config.yaml may manage all repos' README.md and BACKLOG.md only | `not_implemented` | LLM06, ASI03 | AML.T0053, AML.T0085.001, AML.T0081 | The main repo opens with the broad `home` role: repo.outside allow lets it read/write code anywhere, and config.write allow lets it change the control plane. There is no narrow capability limited to README.md/BACKLOG.md across configured r… |
| `R-SCOPE-03` | A conference participant reads only its own structural files (SECURITY.md, DESIGN.md, ARCH.md, README.md, BACKLOG.md), only when asked, and writes only README.md (as a proposal) and BACKLOG.md | `implemented` | LLM06, ASI02 | AML.T0053, AML.T0085.001, AML.T0101 | The worker hook (Claude path) denies every tool except Read/Edit, reads outside `conference.STRUCTURAL_FILES` (HARNESS.md, sibling repos, code) and edits outside `conference.EDITABLE_FILES`. Secrets are scanned in every readable file before the prompt. The OpenCode path enforces the same scope only through its permission rules (`src/agents/opencode.py`), not yet verified against a live OpenCode run. The scope is a hook decision, not a sandbox. |
| `R-SCOPE-04` | No agent role writes the workspace control plane | `not_implemented` | LLM06, ASI03 | AML.T0081, AML.T0053 | A home-role session can write ~/.config/ai-tmux-workspace because config.write is allow and there is no protected path that overrides the role table. The workspace process/installer are not distinguished from agent roles. |
| `R-SCOPE-05` | The main repo from config.yaml is not a development session | `not_implemented` | LLM06, ASI03 | AML.T0053, AML.T0085.001 | The workspace opens the main repo as a normal Claude pane with the normal development toolset. There is no 'management-only' mode or profile for it. |
| `R-POL-01` | One central policy engine, two adapters, no rules in the adapters | `implemented` | LLM06, ASI02, ASI10 | AML.T0053, AML.T0085.001 | — |
| `R-POL-02` | The policy is active for every agent session | `not_implemented` | LLM06, ASI02, ASI10 | AML.T0053, AML.T0081, AML.T0105 | The launcher never exports WS_SESSION_ROLE, so the Claude hook skips the policy. The OpenCode plugin still calls the CLI, which fails closed without a role, so Claude and OpenCode behave differently. |
| `R-POL-03` | The conference runs under a dedicated policy role | `not_implemented` | LLM06, ASI02 | AML.T0053, AML.T0085.001 | The conference worker never declares a role, so the central policy cannot classify its tool calls. The only guard is conference_budget.py, which is called by the Claude hook and not by the OpenCode plugin. |
| `R-POL-04` | Unknown tools and paths fail closed | `implemented` | LLM06, ASI02 | AML.T0053 | — |
| `R-POL-07` | The policy has a protected-path list checked before role capabilities | `not_implemented` | LLM06, ASI03 | AML.T0081, AML.T0053 | There is no protected-path check; the role table is the only decision. A home session can write the control plane and the workspace repo. |
| `R-CONF-01` | One persistent conference per workspace; Esc and Ctrl+X only close the screen | `not_implemented` | LLM04, ASI06, ASI08 | AML.T0080, AML.T0080.001 | The conference is tied to the screen PID. Esc calls stop() and unlinks active.json; there is no resume path. Closing the popup also ends it. |
| `R-CONF-02` | The full conference log goes in every prompt | `not_implemented` | LLM04, ASI06 | AML.T0080, AML.T0143 | The worker prompt is built from the current message only; previous messages and answers are not included. The persisted log is not the prompt context. |
| `R-CONF-03` | /clear archives the log and resets context, keeping the conference | `not_implemented` | LLM04, ASI06 | AML.T0080, AML.T0080.000 | There is no /clear. The only reset is stop + start, which loses the participants and depends on the screen. |
| `R-CONF-06` | Stale waiting/status is cleared on load or resume | `not_implemented` | ASI08, LLM10 | AML.T0029, AML.T0034.002 | A worker killed by a workspace restart leaves awaiting/status in active.json; reopening would show it as waiting forever. There is no per-participant stop (R-CONF-13). |
| `R-CONF-08` | Model output is sanitized before rendering | `not_implemented` | LLM05 | AML.T0077, AML.T0067, AML.T0136 | A model answer or a malicious README can inject ANSI/OSC/control sequences into the TUI; there is no sanitizer. |
| `R-CONF-09` | README.md and BACKLOG.md are scanned for secrets before the external prompt | `not_implemented` | LLM02 | AML.T0024, AML.T0025, AML.T0037 | There is no content scan. A secret in README.md/BACKLOG.md is read by the agent and sent to the provider; it may also be echoed into the persisted log. |
| `R-CONF-10` | Conference participants cannot execute code or use a shell | `implemented` | LLM05, ASI05 | AML.T0050, AML.T0105, AML.T0102 | — |
| `R-CONF-11` | File content is data, not instructions | `not_implemented` | LLM01, ASI01 | AML.T0051.001, AML.T0054, AML.T0093, AML.T0143 | The current prompt encourages reading siblings and the harness and does not establish that file content is untrusted data. The new scope (own README/BACKLOG only) is not reflected. |
| `R-CONF-12` | One answer per message, with a bounded file/tool budget | `not_implemented` | LLM06, LLM10, ASI02 | AML.T0053, AML.T0034.002, AML.T0085.001 | The budget allows 12 distinct files and 20 steps, far more than the two-file/no-turn scope. The prompt asks for one paragraph, but the mechanism permits a long analysis. |
| `R-PERS-01` | All persistent state lives under ~/.config/ai-tmux-workspace | `not_implemented` | LLM02, ASI03 | AML.T0037, AML.T0025 | State is in ~/.cache/workspace and the harness in ~/.local/share; the conference is under the old state dir. The persistence is split, and ~/.cache is not durable by design. |
| `R-PERS-02` | The installed harness moves; the home Harness stays | `not_implemented` | LLM03, ASI04 | AML.T0010, AML.T0010.005 | The installed harness/policy is under ~/.local/share, not the config folder. The home Harness stays, but the read exception is not implemented. |
| `R-PERS-03` | State, config and conference files are 0600/0700 | `not_implemented` | LLM02 | AML.T0037, AML.T0025 | The conference log, active state, lock and prompts use the default umask (often 0644/0755); they can contain repo text and answers. Only the policy log and generated settings are explicitly 0600. |
| `R-SEC-01` | Known secret paths are denied | `implemented` | LLM02 | AML.T0037, AML.T0025, AML.T0055 | — |
| `R-SEC-03` | No literal credentials in config, generated settings or argv | `implemented` | LLM02, LLM03 | AML.T0055, AML.T0083 | — |
| `R-SEC-04` | The provider data handling is an explicit decision | `needs_decision` | LLM02 | AML.T0024, AML.T0086 | There is no explicit acknowledgment before the content leaves the machine. The provider sees the prompt; this is a privacy decision, not a technical control. |
| `R-SEC-05` | A detected secret blocks the run | `not_implemented` | LLM02 | AML.T0024, AML.T0037 | The scan does not exist; the accepted decision is to block the run when a secret is found (D-003), but it is not implemented. |
| `R-ISO-01` | The agent runs in an OS sandbox or under a dedicated user | `not_implemented` | LLM06, ASI05, ASI10 | AML.T0105, AML.T0112, AML.T0112.000 | The agent runs as the same OS user; an allowed program or a compromised CLI can read/write outside the box. The policy cannot see inside an allowed interpreter. |
| `R-HOME-01` | The home role cannot write the control plane or the workspace repo | `not_implemented` | LLM06, ASI03 | AML.T0081, AML.T0053 | The home role's broad repo.outside and config.write allow it to write the workspace repo and the control plane; there is no protected path (R-POL-07). |
| `R-HOME-02` | The main repo role has a narrow README/BACKLOG capability | `not_implemented` | LLM06, ASI03 | AML.T0053, AML.T0085.001 | There is no narrow capability; the `home` role's repo.outside allow is a superset that also permits code and arbitrary files. |
| `R-HOME-03` | The home Harness is readable through a narrow exception | `not_implemented` | LLM06, ASI03 | AML.T0037, AML.T0085.001 | Once the policy is active, a repo session (repo.outside deny) cannot read the file the global CLAUDE.md instructs it to read. The user decided the home Harness stays; the exception is missing. |

## P1 — hardening

| ID | Requirement | Status | OWASP | ATLAS | Failure / reason |
|---|---|---|---|---|---|
| `R-SCOPE-06` | Development happens inside each repo's own folder | `implemented` | LLM06, ASI03 | AML.T0053 | — |
| `R-POL-05` | OpenCode's missing ask channel is fail-closed | `implemented` | LLM06, ASI02 | AML.T0053 | — |
| `R-POL-06` | The audit log is 0600 and contains no raw arguments or secrets | `implemented` | LLM02, ASI03 | AML.T0025, AML.T0037 | — |
| `R-CONF-04` | A stale worker cannot write into a newer conference | `implemented` | LLM04, ASI08 | AML.T0080, AML.T0130 | — |
| `R-CONF-05` | The broker owns the log and its attribution | `implemented` | ASI07, ASI03 | AML.T0073, AML.T0074, AML.T0118.001 | — |
| `R-CONF-07` | The log sent in each prompt has an explicit cap | `not_implemented` | LLM10 | AML.T0029, AML.T0034, AML.T0034.002 | No cap exists. With the new full-log requirement, the prompt can grow without bound. The accepted decision is a hard cap with visible truncation (D-002), but it is not implemented. |
| `R-CONF-13` | A participant can be stopped without ending the conference | `not_implemented` | LLM10, ASI08 | AML.T0029, AML.T0034.002 | A stuck participant holds the header and the cost until 300 s; there is no per-participant stop or circuit breaker. |
| `R-CONF-14` | Participants cannot impersonate Rafael or another participant | `implemented` | ASI07, ASI09 | AML.T0073, AML.T0074, AML.T0118.001 | — |
| `R-CONF-15` | Answers are grounded in the two permitted files | `not_implemented` | LLM09, ASI09 | AML.T0060, AML.T0062, AML.T0130 | The prompt does not require citations or an explicit 'the files do not answer this'; the model can answer confidently without reading the two files. |
| `R-CONF-17` | README writes require a diff and confirmation | `not_implemented` | LLM05, LLM06, ASI09 | AML.T0101, AML.T0067 | There is no README write today; the new scope adds it. The accepted decision is to show the diff and wait for confirmation (D-004), but it is not implemented. |
| `R-PERS-04` | Moving the paths does not orphan existing state | `not_implemented` | LLM03, ASI04 | AML.T0010 | No migration exists; changing the defaults would orphan the old state/harness. The installer manifest may restore an old path and miss the new one. |
| `R-SEC-02` | Content of arbitrary files is not scanned before normal-session prompts | `needs_decision` | LLM02 | AML.T0024, AML.T0086 | The policy is path-level; a secret in a non-secret-named file inside the repo is not detected. Scanning every file a normal session reads is expensive and may be noisy. |
| `R-SC-01` | Agent CLIs are pinned and verified; no curl\|bash by default | `not_implemented` | LLM03, ASI04 | AML.T0010, AML.T0010.005, AML.T0109, AML.T0111 | The installer pipes a remote script to bash and records no pinned version/hash; a compromised upstream or a supply-chain substitution executes code. |
| `R-SC-02` | The installer has a manifest, backup and killswitch | `implemented` | LLM03, ASI04 | AML.T0010, AML.T0109 | — |
| `R-ISO-02` | Known policy bypasses are documented in the repo | `not_implemented` | LLM06, ASI05 | AML.T0053, AML.T0105 | The limits are known and partly tested, but they live in a local handoff; a user reading the README would think the policy is a boundary. |
| `R-ISO-03` | A killswitch stops the workspace and restores the install | `implemented` | ASI10 | AML.T0103, AML.T0105 | — |

## P2 — lower priority

| ID | Requirement | Status | OWASP | ATLAS | Failure / reason |
|---|---|---|---|---|---|
| `R-CONF-16` | System prompt leakage is low impact | `implemented` | LLM07 | AML.T0069, AML.T0069.002, AML.T0085.003 | — |
| `R-PERS-05` | The conference log has a retention/backup policy | `needs_decision` | LLM02 | AML.T0037 | There is no retention or backup policy; the log can be synced by a backup tool and kept indefinitely. |
| `R-SC-03` | ACP is not a runtime dependency; if adopted, it is pinned | `implemented` | LLM03, ASI04 | AML.T0010.005, AML.T0115.002 | — |

## Out of scope

| ID | Requirement | Status | OWASP | ATLAS | Failure / reason |
|---|---|---|---|---|---|
| `R-OOS-01` | Vector and embedding weaknesses (LLM08) | `out_of_scope` | LLM08 | AML.T0070, AML.T0071, AML.T0066 | Out of scope: no vector/embedding system is used. |
| `R-OOS-02` | Model weights and provider-side training poisoning (LLM04) | `out_of_scope` | LLM04 | AML.T0018, AML.T0020 | Out of scope: the provider owns the model; the workspace controls the context, not the weights. |
| `R-OOS-03` | OS, kernel, tmux and terminal CVEs | `out_of_scope` | — | — | Out of scope: platform-level; no OWASP LLM/ATLAS mapping. |
| `R-OOS-04` | Multi-user and multi-tenant isolation | `out_of_scope` | — | — | Out of scope: one person, one OS user. |
| `R-OOS-05` | Adjacent surfaces: GitHub API, browser and mobile clients | `out_of_scope` | — | — | Out of scope: they do not process the agent's prompt/context. |
| `R-OOS-06` | Physical access | `out_of_scope` | — | — | Out of scope: physical security. |
| `R-OOS-07` | Upstream vendor internal compromise beyond pinning | `out_of_scope` | LLM03, ASI04 | AML.T0010, AML.T0109 | Out of scope: the vendor's internal pipeline; pinning/verification is the boundary we can enforce. |
| `R-OOS-08` | The provider's internal retention/training | `out_of_scope` | LLM02 | AML.T0024, AML.T0086 | Out of scope: provider-internal handling; the person's send decision is R-SEC-04. |

## Open questions (needs_decision)

| Requirement | Question | Options | Recommended |
|---|---|---|---|
| `R-PERS-05` | How long should the conference log be kept, and is it backed up? | 30 days, excluded from cloud backups; Keep until /clear; No retention, delete on /clear | 30 days, excluded from cloud backups |
| `R-SEC-02` | Should normal sessions scan file content for secrets before sending to the provider? | Only the conference; No, path-level only; Yes, for configured patterns | Only the conference |
| `R-SEC-04` | May README.md/BACKLOG.md content leave the machine to the configured provider? | Ask per conference; Yes, per profile, documented; No, local model only for conference | Ask per conference |

## Prioritization questions

| Requirement | Question | Options | Recommended |
|---|---|---|---|
| `R-ISO-01` | When should the OS sandbox (item 69 / dedicated user 35/40) be prioritized relative to the conference P0 work? | Before broad home/main access; After the conference P0; Now | Before broad home/main access |
| `R-SC-03` | When should ACP be revisited? | When the Claude adapter supports --settings/apiKeyHelper; When OpenCode is the primary agent; Never; keep the internal adapter | When the Claude adapter supports --settings/apiKeyHelper |

## OWASP coverage

| ID | Rollup | Requirements |
|---|---|---|
| `LLM01` | `not_implemented` | R-CONF-11 |
| `LLM02` | `not_implemented` | R-POL-06, R-CONF-09, R-PERS-01, R-PERS-03, R-PERS-05, R-SEC-01, R-SEC-02, R-SEC-03, R-SEC-04, R-SEC-05, R-OOS-08 |
| `LLM03` | `not_implemented` | R-PERS-02, R-PERS-04, R-SEC-03, R-SC-01, R-SC-02, R-SC-03, R-OOS-07 |
| `LLM04` | `not_implemented` | R-CONF-01, R-CONF-02, R-CONF-03, R-CONF-04, R-OOS-02 |
| `LLM05` | `not_implemented` | R-CONF-08, R-CONF-10, R-CONF-17 |
| `LLM06` | `not_implemented` | R-SCOPE-01, R-SCOPE-02, R-SCOPE-03, R-SCOPE-04, R-SCOPE-05, R-SCOPE-06, R-POL-01, R-POL-02, R-POL-03, R-POL-04, R-POL-05, R-POL-07, R-CONF-12, R-CONF-17, R-ISO-01, R-ISO-02, R-HOME-01, R-HOME-02, R-HOME-03 |
| `LLM07` | `implemented` | R-CONF-16 |
| `LLM08` | `out_of_scope` | R-OOS-01 |
| `LLM09` | `not_implemented` | R-CONF-15 |
| `LLM10` | `not_implemented` | R-CONF-06, R-CONF-07, R-CONF-12, R-CONF-13 |
| `ASI01` | `not_implemented` | R-CONF-11 |
| `ASI02` | `not_implemented` | R-SCOPE-03, R-POL-01, R-POL-02, R-POL-03, R-POL-04, R-POL-05, R-CONF-12 |
| `ASI03` | `not_implemented` | R-SCOPE-01, R-SCOPE-02, R-SCOPE-04, R-SCOPE-05, R-SCOPE-06, R-POL-06, R-POL-07, R-CONF-05, R-PERS-01, R-HOME-01, R-HOME-02, R-HOME-03 |
| `ASI04` | `not_implemented` | R-PERS-02, R-PERS-04, R-SC-01, R-SC-02, R-SC-03, R-OOS-07 |
| `ASI05` | `not_implemented` | R-CONF-10, R-ISO-01, R-ISO-02 |
| `ASI06` | `not_implemented` | R-CONF-01, R-CONF-02, R-CONF-03 |
| `ASI07` | `implemented` | R-CONF-05, R-CONF-14 |
| `ASI08` | `not_implemented` | R-CONF-01, R-CONF-04, R-CONF-06, R-CONF-13 |
| `ASI09` | `not_implemented` | R-CONF-14, R-CONF-15, R-CONF-17 |
| `ASI10` | `not_implemented` | R-POL-01, R-POL-02, R-ISO-01, R-ISO-03 |

## ATLAS coverage

| ID | Rollup | Requirements |
|---|---|---|
| `AML.T0010` | `not_implemented` | R-PERS-02, R-PERS-04, R-SC-01, R-SC-02, R-OOS-07 |
| `AML.T0010.005` | `not_implemented` | R-PERS-02, R-SC-01, R-SC-03 |
| `AML.T0018` | `out_of_scope` | R-OOS-02 |
| `AML.T0020` | `out_of_scope` | R-OOS-02 |
| `AML.T0024` | `not_implemented` | R-CONF-09, R-SEC-02, R-SEC-04, R-SEC-05, R-OOS-08 |
| `AML.T0025` | `not_implemented` | R-POL-06, R-CONF-09, R-PERS-01, R-PERS-03, R-SEC-01 |
| `AML.T0029` | `not_implemented` | R-CONF-06, R-CONF-07, R-CONF-13 |
| `AML.T0034` | `not_implemented` | R-CONF-07 |
| `AML.T0034.002` | `not_implemented` | R-CONF-06, R-CONF-07, R-CONF-12, R-CONF-13 |
| `AML.T0037` | `not_implemented` | R-POL-06, R-CONF-09, R-PERS-01, R-PERS-03, R-PERS-05, R-SEC-01, R-SEC-05, R-HOME-03 |
| `AML.T0050` | `implemented` | R-CONF-10 |
| `AML.T0051.001` | `not_implemented` | R-CONF-11 |
| `AML.T0053` | `not_implemented` | R-SCOPE-01, R-SCOPE-02, R-SCOPE-03, R-SCOPE-04, R-SCOPE-05, R-SCOPE-06, R-POL-01, R-POL-02, R-POL-03, R-POL-04, R-POL-05, R-POL-07, R-CONF-12, R-ISO-02, R-HOME-01, R-HOME-02 |
| `AML.T0054` | `not_implemented` | R-CONF-11 |
| `AML.T0055` | `implemented` | R-SEC-01, R-SEC-03 |
| `AML.T0060` | `not_implemented` | R-CONF-15 |
| `AML.T0062` | `not_implemented` | R-CONF-15 |
| `AML.T0066` | `out_of_scope` | R-OOS-01 |
| `AML.T0067` | `not_implemented` | R-CONF-08, R-CONF-17 |
| `AML.T0069` | `implemented` | R-CONF-16 |
| `AML.T0069.002` | `implemented` | R-CONF-16 |
| `AML.T0070` | `out_of_scope` | R-OOS-01 |
| `AML.T0071` | `out_of_scope` | R-OOS-01 |
| `AML.T0073` | `implemented` | R-CONF-05, R-CONF-14 |
| `AML.T0074` | `implemented` | R-CONF-05, R-CONF-14 |
| `AML.T0077` | `not_implemented` | R-CONF-08 |
| `AML.T0080` | `not_implemented` | R-CONF-01, R-CONF-02, R-CONF-03, R-CONF-04 |
| `AML.T0080.000` | `not_implemented` | R-CONF-03 |
| `AML.T0080.001` | `not_implemented` | R-CONF-01 |
| `AML.T0081` | `not_implemented` | R-SCOPE-02, R-SCOPE-04, R-POL-02, R-POL-07, R-HOME-01 |
| `AML.T0083` | `implemented` | R-SEC-03 |
| `AML.T0085.001` | `not_implemented` | R-SCOPE-01, R-SCOPE-02, R-SCOPE-03, R-SCOPE-05, R-POL-01, R-POL-03, R-CONF-12, R-HOME-02, R-HOME-03 |
| `AML.T0085.003` | `implemented` | R-CONF-16 |
| `AML.T0086` | `needs_decision` | R-SEC-02, R-SEC-04, R-OOS-08 |
| `AML.T0093` | `not_implemented` | R-CONF-11 |
| `AML.T0101` | `not_implemented` | R-SCOPE-03, R-CONF-17 |
| `AML.T0102` | `implemented` | R-CONF-10 |
| `AML.T0103` | `implemented` | R-ISO-03 |
| `AML.T0105` | `not_implemented` | R-POL-02, R-CONF-10, R-ISO-01, R-ISO-02, R-ISO-03 |
| `AML.T0109` | `not_implemented` | R-SC-01, R-SC-02, R-OOS-07 |
| `AML.T0111` | `not_implemented` | R-SC-01 |
| `AML.T0112` | `not_implemented` | R-ISO-01 |
| `AML.T0112.000` | `not_implemented` | R-ISO-01 |
| `AML.T0115.002` | `implemented` | R-SC-03 |
| `AML.T0118.001` | `implemented` | R-CONF-05, R-CONF-14 |
| `AML.T0130` | `not_implemented` | R-CONF-04, R-CONF-15 |
| `AML.T0136` | `not_implemented` | R-CONF-08 |
| `AML.T0143` | `not_implemented` | R-CONF-02, R-CONF-11 |

## Assumptions

- The workspace process is trusted; the agent processes are untrusted by default.
- The policy is an in-process guardrail, not an OS sandbox; the OS user is the same for the workspace and the agents.
- README.md/BACKLOG.md and other repo content are untrusted input, not instructions.
- The broker is the only writer of the conference log; the model cannot set sender/attribution.
- The window profile is the source of the agent selection (Claude or OpenCode).
- The installed harness is trusted after install; supply-chain pinning is a separate control.
- External providers receive the prompt; the person's send decision is R-SEC-04.
- The `home` Harness stays in the home repo; a narrow read exception is required.
- The main folder is the main repo from config.yaml (today ~/Codes/home); it manages configured repos' README.md/BACKLOG.md only. The control plane is workspace-only (D-005).
- The repo box is the repo plus /tmp; outside the repo, only /tmp is allowed (D-001).
- No RAG/vector store is used.

## Validation plan

Each requirement has its own `validation` field in the JSON. The minimum suite:

1. **Policy role matrix**: `conference` allows `Read`/`Edit` on `README.md`/`BACKLOG.md`
   only; denies `Grep`, `Glob`, `Bash`, `Write`, siblings, `~/.config`, `~/.ssh`, `/tmp`,
   unknown tools, and symlink escapes. A repo session may use `/tmp` (D-001); a conference
   participant may not.
2. **Adapter parity**: the same action battery through the Claude hook and the OpenCode
   plugin returns the same decision.
3. **Protected paths**: `home` cannot write the control plane or the workspace repo; a
   `repo` session rooted there can; the workspace process/installer can. The main repo
   cannot write code (D-005).
4. **Prompt injection**: a fixture `README.md` with "ignore previous instructions", a
   `~/.ssh` read, a `/tmp` write and ANSI escapes; the run is contained and the output is
   sanitized.
5. **Secrets**: a fake token in `README.md`/`BACKLOG.md` blocks the run (D-003); the policy
   log has no token.
6. **Persistence**: Esc/reopen, workspace restart, `/clear`, late worker; the log survives,
   the stale waiting marks clear, the old log is archived.
7. **Full log**: the second prompt contains the first message and answers; the hard cap
   truncates with a visible note (D-002).
8. **Window profile**: a Claude window runs Claude; an OpenCode window runs OpenCode; the
   global `agent` does not override the window.
9. **Cost/timeout**: a hanging worker is killed at `TIMEOUT_S`; the note is visible; the
   others continue; the per-participant stop works.
10. **README write**: the proposed diff is shown and the write waits for confirmation
    (D-004); BACKLOG.md follows the current "only when asked" rule.
11. **Audit**: every denied tool call has a `policy.jsonl` entry with the role and a
    correlation id; no raw secret.

## References

- OWASP Top 10 for LLM Applications 2025 — https://genai.owasp.org/llm-top-10/
- OWASP Top 10 for Agentic Applications, Dec 2025 —
  https://genai.owasp.org/2025/12/09/owasp-genai-security-project-releases-top-10-risks-and-mitigations-for-agentic-ai-security/
- MITRE ATLAS v2026.10 —
  https://github.com/mitre-atlas/atlas-data/releases/tag/v2026.10
- Repo references: `README.md` (Segurança), `harness/policy.yaml`, `src/policy.py`,
  `src/hook.py`, `src/conference.py`, `src/conference_worker.py`,
  `src/conference_budget.py`, `bin/workspace`, `.claude/handoff.md`.
