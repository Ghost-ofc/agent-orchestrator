# Changelog

## Unreleased

- Review fixes for continuous execution: deleting a plan now also disables its supervision, and the
  supervisor ignores deleted/dismissed plans; auto-advance no longer forces `--allow-dirty` or
  concurrent writers (one job per workspace); supervised acceptance again requires every claimed check
  to be a command the reviewer ran with exit code 0 (exact match; Antigravity success parsed from the
  reported code, not prose); changing a plan's executor pool needs `confirm` and never approves a
  protected expensive model implicitly; task dependencies must reference real tasks and be acyclic;
  batch contracts reject unauthorized or re-pinned executors instead of picking one at random and the
  raw log tail is no longer a contract source for structured event streams; deleting a job never
  removes its audit directory; SSH audit events record the command, and `root` destinations need
  `allow_root: true`; routing of sensitive worker questions covers ssh/root/sudo/prod.
- Fix the Antigravity adapter: it passed `--print` before `--input-format`, but in `agy` `--print` takes
  the prompt as its value and swallowed the next flag, so every run failed. The documented stdin
  stream-json form needs no `--print`. No permission-bypass flag is added.
- Fix the OpenCode adapter, which used the removed legacy CLI (`--cwd`, `--quiet`, `--prompt`) and could
  not select a model, so free models were unusable. It now runs `opencode run --format json --dir <ws>
  --model provider/model` with the task on stdin (no longer visible in the process list), and supports
  `--agent` and `--variant`.
- Workers started by the OpenCode adapter get a private `XDG_CONFIG_HOME` per job (new `env` profile key,
  validated: no `PATH`, `LD_*`, `HOME`, and similar). The person's global MCP servers pushed the tool count
  past the 128 a free model's upstream accepts and exposed unrelated credentials to workers.
- Show OpenCode and Antigravity output in the panel (messages, tool calls, written files) and report
  OpenCode usage by summing its per-step token and cost events.
- Redesign the local dashboard as a control center: a global status summary, an attention tray for
  worker questions and pending reviews, and a pannable, zoomable canvas that draws project, plan,
  and job relationships only from recorded data, with an equivalent accessible list view.
- Make Spanish the default interface language through a central dictionary (`web/i18n.js`).
  Runner records, worker messages, logs, file paths, IDs, and unknown identifiers stay verbatim.
- Present the inspector timeline as a conversation and keep selection, focus, camera position, and
  answer drafts stable across the two-second refresh.
- Serve a dedicated favicon and the new `i18n.js` through the existing exact-path static allowlist;
  the loopback-only boundary, token authentication, and CSP are unchanged.
- Add tests that every interface string key and every runner state or event has a Spanish label,
  and that animation stays on `transform`/`opacity`.
- Show what the agent actually did: a read-only transcript endpoint (`/api/projects/<id>/jobs/<id>/transcript`)
  extracts messages, tool calls, and the final result from `stdout.log` (Claude `stream-json`, Codex
  `--json`, plain text), bounded in size and cached by an etag. Model reasoning is never displayed.
- Explain empty sections instead of printing "Unavailable": no Git baseline, files written through agent
  tools, review notes and test evidence, deduplicated usage with cost, duration, turns, and cache, and the
  runner's error output as the failure reason.
- Codex jobs in a workspace that is not a Git repository now pass `--skip-git-repo-check` (new
  `non_git_args` profile key). The Codex sandbox stays `workspace-write`; only the trusted-directory
  startup check is skipped. Previously these jobs failed immediately.
- Act from the dashboard: cancel a running job, record a review, create a plan, launch a plan item, and
  retry a failed or rejected item. Every change is delegated to the engine CLI in a subprocess (new
  `panel_actions.py`), so executor approval, expensive-model protection, task contracts, attempt limits,
  workspace locks, and review evidence rules are enforced by the engine and its rejection text is shown
  verbatim. Requests are strictly validated, free-text values are passed as `--option=value`, and user text
  cannot forge plan headings or checklist items.
- An accepted review still requires at least one independently run test result, and an accepted review is
  immutable; launching requires an explicit confirmation of the executor and its cost.
- New routes: `GET /api/executors`, `GET /api/projects/<id>/plans/<id>`, `GET .../jobs/<id>/launch-template`,
  and `POST` for `.../jobs/<id>/cancel`, `.../jobs/<id>/review`, `.../plans`, and
  `.../plans/<id>/items/<id>/launch`. Authentication, loopback origin checks, and the 64 KiB limit for question
  answers are unchanged; actions accept up to 256 KiB.

## 0.7.1 - 2026-09-14

- Treat an explicit Agent Orchestrator invocation as a request for a native Codex persistent goal,
  unless the user opts out.
- Create a native goal only when none is unfinished, reuse an aligned existing goal, and stop for
  user direction instead of replacing an unrelated active goal.
- Keep detailed execution state in the durable plan and complete the native goal only after plan,
  review, validation, and owned-cleanup gates all pass.

## 0.7.0 - 2026-09-14

- Add a sandboxed Cursor CLI adapter with live model discovery and explicit model selection.
- Bundle a Cursor hook that denies native subagent spawning so one orchestrator job remains one
  observable, accountable agent; continue using Grok's native `--no-subagents` control.
- Add durable `auto`, `single`, and `cross-harness` team topology plus a bounded parallel-worker
  limit to every plan.
- Define cross-harness teams as the product boundary: Agent Orchestrator owns delegation, routing,
  questions, usage, retries, review, and escalation across independent CLI subscriptions.
- Document the Fusion-inspired lead/sidekick pattern while avoiding hidden model switches, swarm
  overhead, and unverified subscription or cost claims.

## 0.6.0 - 2026-09-14

- Group linked worktree jobs under their canonical repository in the dashboard, add a selected
  project's inner agent navigation, and reserve project alerts for questions requiring user input.
- Remember an explicit opt-out from Git worktrees and capture the selected workspace mode in every
  new durable plan.
- Add task-type-aware executor recommendations that combine transparent, versioned public
  benchmark/model priors with minimum-sample reviewed local outcomes while preserving user pools
  and the explicit Astra boundary.
- Add automatic intent routing: `cost-first` by default, `quality-first` for explicit
  quality-over-cost requests, and `maximum-quality` only for explicit frontier authorization.
- Let quality-first plans automatically use installed GPT-5.6 Sol and Claude Opus executors while
  continuing to exclude Astra; only maximum-quality can place Astra in an automatic pool.
- Classify plans by low, medium, or high risk and persist each executor's rank, reasoning effort,
  rationale, and authorization source. Plan-bound launches inherit the recorded effort.
- Store the plan's Goal as a durable goal ledger and add `plan-checkpoint` for compact continuation
  state instead of replaying full coordinator history and raw logs.
- Add recent per-executor outcome and provider-usage aggregation with `metrics --since-hours 24`.
- Document evidence from a long-running trace showing coordinator context replay—not bounded
  executor input—was the dominant token amplifier, and require event-driven monitoring.
- Add GPT-5.6 Sol to the built-in Codex CLI model catalog while retaining custom executor pools,
  review gates, scope controls, dashboard feedback, and the no-silent-Astra fallback rule.

## 0.5.0 - 2026-09-14

- Make executor routing fail closed: built-in routes are recommendations only, model-capable CLIs
  require an explicit model, and every launch must match a durable pool or one-off approval.
- Require separate, visible cost approval before Astra, Fable, Opus, or another marked frontier
  model may execute code; planning and review never imply executor approval.
- Add durable plan executor pools, replacement history, and `executor-options` attempt/exhaustion
  reporting so failed or timed-out workers can only move to another approved entry.
- Add optional pre-approved Terra fallback after pool exhaustion, a linked unanswered user-feedback
  grace period, and a 32 KiB focused context limit. Astra is never an automatic fallback.
- Require escalation questions in both the active Codex chat and the dashboard, with user answers
  taking precedence over timeout behavior.
- Add `ensure-dashboard` to automatically start or reuse one private all-project dashboard at the
  beginning of every orchestration session.
- Add a Google Antigravity CLI profile using the official sandboxed headless JSONL protocol, with
  explicit model, effort, and internal-agent selection plus live model/agent discovery.

## 0.4.0 - 2026-09-13

- Add a dependency-free loopback web dashboard for all projects and jobs, with a continuous agent
  table, inspector tabs, two-second refresh, filters, and preserved selections and answer drafts.
- Add durable project orchestrator feedback and JSON `request-feedback`, `feedback`, and
  `answer-feedback` commands. Answer both worker and orchestrator questions through shared locked,
  audited transitions from the CLI or dashboard.
- Protect the local HTTP interface with a per-server capability token, loopback Host/Origin checks,
  strict static routes, response security headers, and bounded validated JSON mutations.
- Default coordinator identity truthfully to `current-codex-task`; route recommendations never
  replace the current task model or an explicit coordinator override.
- Narrow the Codex executor write grant to each job's communication subtree with `--add-dir`,
  retaining `workspace-write`, authoritative records outside that subtree, and legacy question reads.
- Add an explicit post-acceptance cleanup gate for orchestrator-created temporary resources while
  preserving user-owned files and durable audit history by default.

## 0.3.1 - 2026-09-13

- Point the Codex plugin details page's Website action at the public GitHub repository.

## 0.3.0 - 2026-09-13

- Add user-selectable `quality-first` and `economy-first` coordinator/executor routes plus custom
  model-role overrides.
- Add an installed-harness chooser and compact live Codex model discovery; surface configured Claude
  model aliases and each harness's supported controls.
- Add durable plans, decision records, checklist-bound jobs, and resumable plan status.
- Enforce a 64 KiB context-capsule budget for built-in routes with planning guidance for larger
  work.
- Add reasoning-effort controls and machine-readable output for Codex CLI and Claude Code.
- Capture provider-reported token/cost usage when present.
- Require an independent review verdict and test evidence before acceptance or dependency release.

## 0.2.0 - 2026-09-13

- Publish Agent Orchestrator as an installable Codex plugin marketplace.
- Add built-in DeepSeek Harness, Kimi Code, Codex CLI, and Claude Code profiles.
- Add compatible executable fallback selection for Kimi Code.
- Surface adapter maturity, documentation, and installation guidance.
- Include detached execution, structured task contracts, worker questions, live observability,
  scope controls, notifications, and Codex review/repair guidance.
