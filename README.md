# Agent Fleet

A small harness that turns your **issue tracker into a work queue for autonomous coding agents**. A
dispatcher polls for human-approved issues, and runs one sandboxed headless agent per issue in its own
git worktree. Each agent can **read production** and **read+write staging**, but **cannot** write
production, merge `main`, or deploy production — those capabilities don't exist in its environment.

```
tracker "AI Ready" ──▶ dispatcher claims ──▶ headless agent (own worktree) ──▶ "AI Review" / "AI Blocked"
                       (status = the queue)    context → fix → audit → push        + a notification to you
```

The *why* and the safety model are in [`DESIGN.md`](DESIGN.md). This file gets you running on the
reference stack, then shows how to retarget it to a different one.

---

## The reference stack

This repo ships wired to one concrete stack. Everything stack-specific lives in a single small,
self-contained file per "plane" — the dispatcher *core* (claim → lease → worktree → parse result)
never changes. That's what makes it adaptable: you replace planes, not the engine.

| Plane | Ships configured for | Lives in | Swap difficulty |
|---|---|---|---|
| Issue tracker (the queue) | **Linear** (GraphQL) | `dispatcher/linear_api.py` | reimplement 5 funcs |
| Coding agent | **Claude Code** CLI, headless | `dispatcher/runner.py` + `worker-prompt.md` | usually keep |
| Database access | **Postgres** via `psql` | `mcp/agent_rds_mcp.py` | change the client cmd |
| Staging orchestration | **Kubernetes / EKS** via `kubectl` | `mcp/eks_staging.sh` | edit 5 case branches |
| Version control | **GitHub** via `gh` + a GitHub App | `mcp/github_mcp.py` | swap CLI + token mint |
| Cloud perimeter | **AWS IAM** (scoped role, prod-deny) | `infra/iam-agent.tf` | re-express per cloud |
| Observability | **AWS CloudWatch** (read-only MCP) | `agent.mcp.json` | swap the MCP entry |
| Notifications | **Resend** email | `dispatcher/notify.py` | change one HTTP POST |

All configuration is environment-driven — see [`example.env`](example.env). Nothing about your
infrastructure is hardcoded.

## How it works (30-second model)

1. **The tracker status is the durable queue.** A human moves an issue to `AI Ready` (entry gate).
   Nothing is worked unless it's there.
2. The **dispatcher** (`dispatcher/dispatcher.py`, one stdlib process) polls `AI Ready`, claims an
   issue by moving it to `In Progress` (the status move *is* the lock), and spawns a worker.
3. The **worker** runs as a low-privilege user in an isolated worktree off `main`, following the
   methodology in `worker-prompt.md`, using only the scoped MCP tools in `agent.mcp.json`. It ends by
   printing a machine-readable `RESULT: {…}` line.
4. The dispatcher parses that line → `AI Review` (success, human reviews the PR) or `AI Blocked`
   (stuck — a human queue) → sends a notification. A lease sweeper recovers crashed runs.

---

## Quickstart (reference stack)

**Prerequisites:** `python3`, the `claude` CLI, plus the reference-stack CLIs your tools call —
`psql`, `kubectl`, `gh`, `aws`, `terraform`. A Linear team, a GitHub App, and an AWS account with a
scoped role.

```bash
# 1. Configure — copy the template, fill in your values, keep the real file out of git.
cp example.env /etc/agent/dispatcher.env && $EDITOR /etc/agent/dispatcher.env

# 2. Create two workflow statuses in your Linear team: "AI Ready" (entry) and "AI Blocked"
#    (escalation). Paste their ids — plus the existing In-Progress / AI-Review ids — into STATUS_*.

# 3. Apply the scoped cloud role (prod-deny). Supply your own -var values.
cd infra && terraform apply   # account, region, cluster, rds instances, principals, namespace

# 4. Run the dispatcher.
sudo cp systemd/agent-dispatcher.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now agent-dispatcher
journalctl -u agent-dispatcher -f

# 5. Move a low-risk issue into "AI Ready" and watch it flow.
```

The dispatcher fails fast at startup if any required value is missing: `LINEAR_API_KEY`,
`LINEAR_TEAM_KEY`, `AGENT_LINEAR_USER_ID`, and the four `STATUS_*` ids. Everything else has a working
default in `example.env`.

Sanity-check any MCP tool by hand:
```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 mcp/eks_staging_mcp.py
```

---

## Adapting to your stack

The dispatcher core is stack-agnostic. To retarget, replace only the planes that differ — each is a
small, isolated unit. Adaptations come in two flavors: **(A)** point an env var at your CLI, or **(B)**
reimplement one short file against a documented contract.

### Issue tracker — not Linear? *(reimplement one file)*
The dispatcher only ever calls **five functions** in `dispatcher/linear_api.py`:
`list_by_status`, `get_issue`, `update_state`, `comment`, `try_claim`. Reimplement that ~70-line file
against Jira, GitHub Issues, or anything with statuses + an API. The `STATUS_*` env vars become
whatever your tracker uses to identify a workflow state. Keep `try_claim`'s compare-and-set semantics
(only transition if still in `AI Ready` and unassigned) — that's the lock that prevents double-work.

### Database — not Postgres? *(change the client command)*
- **Prod (read-only) path:** edit the `psql` invocation in `_run_prod_ro()` to your client
  (`mysql`, `sqlite3`, `bq`, …). **The read-only guarantee must come from your DB grants + a
  read-only connection**, exactly as the Postgres default leans on a `SELECT`-only user plus
  `default_transaction_read_only=on`. The tool must have no write override.
- **Staging (read/write) path:** already a command you supply — set `APP_REPO` + `AGENT_RDS_HELPER`
  to your own SQL helper script. Targets come from `AGENT_DB_TARGETS`.

### Staging orchestration — not Kubernetes? *(edit 5 case branches)*
`mcp/eks_staging.sh` is a thin switch over five verbs: `status`, `wake`, `sleep`, `restart`, `logs`.
Replace each branch's `kubectl` line with your orchestrator — `docker compose`, `nomad`, `systemctl`,
ECS, etc. The Python MCP wrapper and the tool schema don't change. **Preserve the boundary:** pin
operations to one fixed environment chosen at deploy time (`STAGING_NAMESPACE` here) — never let the
target be caller-supplied per request.

### Version control — not GitHub? *(swap CLI + token mint)*
`mcp/github_mcp.py` wraps `gh` authenticated as a GitHub App. For GitLab, swap `gh`→`glab` and the
App-token mint for a project access token; keep the same three actions (`pr_create`, `pr_checks`,
`merge_staging`) and the hard pin that the merge target is the **staging** branch, never `main`. The
real guarantee — the bot identity *cannot* merge `main` — must be enforced by your platform's branch
protection, not just the tool.

### Cloud perimeter — not AWS? *(re-express the principle)*
`infra/iam-agent.tf` is the AWS expression of one idea: a **deny-by-default scoped role with an
explicit production deny** that beats any allow. On GCP/Azure, express the same with a service account
+ a deny policy / custom role. The principle is the contract; the Terraform is just one encoding of it.

### Notifications — not Resend? *(change one HTTP POST)*
`dispatcher/notify.py` `_send()` is a single HTTP request. Repoint it at SMTP, a Slack webhook, or
anything — or leave `RESEND_API_KEY` empty to disable notifications entirely (they're best-effort).

### Observability
Replace the `cloudwatch` server entry in `agent.mcp.json` with your provider's read-only MCP (or drop
it). The worker treats it as just another read-only tool.

---

## The one thing you must not break

Whatever you swap, preserve the **three-layer permission boundary** (full rationale in `DESIGN.md`):

1. **Cloud:** an explicit deny on every production resource that overrides any allow.
2. **VCS:** branch protection excludes the agent identity from `main`.
3. **Tools:** the agent gets only scoped MCP tools — no raw `aws`/`kubectl`/`psql`/`gh` — and prod
   tools are read-only by construction.

A change is a security *bug* if it adds a broad credential to the agent's environment, puts the agent
identity in `main`'s allowed-merge set, or hands the agent a raw cloud/DB CLI.
