# Agent Fleet — Issue-Driven Autonomous SWE with Human Oversight

A small, dependency-light harness that turns an issue tracker into the queue for a fleet of
autonomous coding agents. Each issue is worked end-to-end by one headless agent run in an isolated
git worktree; humans gate what *enters* the pipeline and what *leaves* it; everything in between is
bounded by infrastructure, not by prompt instructions.

This document describes the **pattern** and why it's safe to run. It is intentionally generic — all
site-specific identifiers (accounts, hosts, cluster, team, repo) are supplied via configuration (see
[`example.env`](example.env) and [`README.md`](README.md)).

## 1. What this is (and is not)

- **It is:** issue-driven autonomous software engineering with two non-negotiable human gates (entry
  and exit) and defense-in-depth permissions.
- **It is not:** unattended production deployment, raw cloud access, or open-ended autonomy. A human
  gates what enters the pipeline and what merges to production.

## 2. The four building blocks

### a. The issue tracker is the queue
The issue's **status field** is the durable work state. An issue always sits in exactly one column
with a full comment audit trail, so a crash loses nothing — the issue stays where it was and the
sweeper or the next poll picks it up. The harness is replaceable; the tracker holds the truth. This
implementation uses Linear (a tiny stdlib GraphQL client, `dispatcher/linear_api.py`), but any
tracker with a status field and an API fits.

```
 Backlog ──▶ AI Ready ──▶ In Progress ──▶ AI Review ──▶ (human review / merge / deploy)
  (raw)     (human gate:  (claimed by      (success
            approved for   one agent run;   exit gate)
            autonomy)      implement+audit)     ▲
                                │                │ pass
                                └──▶ AI Blocked ─┘ (stuck / lease expired / denied)
```

Two **new** statuses do the work: **AI Ready** (the only place issues are picked up — the entry
gate) and **AI Blocked** (a visible human queue for anything the agent couldn't finish safely).
Everything else reuses the team's existing workflow.

### b. Sandboxed multi-agent workers
The dispatcher is a single long-running loop (`dispatcher/dispatcher.py`). Each tick it:

1. **Sweeps** — In-Progress issues idle past the lease → AI Blocked (recoverable; a human re-queues).
2. **Claims** — up to `CONCURRENCY` issues in AI Ready, moving status AI Ready → In Progress as the
   lock (compare-and-set: the loser of a race backs off).
3. **Works** — spawns one headless agent per claimed issue, each in **its own git worktree** off the
   latest `main`, as a dedicated low-privilege OS user.
4. **Lands** — parses the worker's machine-readable `RESULT` line → AI Review (success) or AI Blocked
   → sends a notification email.

The worker follows a standardized methodology (`worker-prompt.md`, templated per issue): gather
context, post a preliminary review, implement the minimal change, run the repo's checks, self-audit
against the acceptance criteria, push a branch + open a PR, and report. Worktree isolation means
concurrent workers never touch each other's files, and none can touch the dispatcher's checkout or
secrets.

### c. App-token git identity
The agent commits and opens PRs under a **dedicated bot identity** — a GitHub App whose installation
token is minted on demand from a private key held by the git MCP server (`mcp/github_mcp.py`), never
exposed to the model. The App's capabilities are deliberately narrow: push feature branches, open PRs
to `main` (for human review), read CI status, and merge into a **staging** branch (hard-pinned — the
tool cannot target `main`). Because the App identity is *not* in `main`'s branch-protection
allow-list, even a misused token cannot merge to `main` — the platform enforces it independently of
the tool.

### d. Scoped-role boundary
All cloud capability arrives through **purpose-built MCP tools**, each holding a scoped cloud role —
the model never sees credentials and has no raw `aws`/`kubectl`/`psql`/`gh`. The tools are
**staging-write / prod-read by construction**:

| MCP tool | Posture | Boundary |
|---|---|---|
| `agent-rds` | prod **read-only**, staging **read/write** | Prod via a direct SELECT-only DB user; staging via a pod-based SQL helper. No write override exists for prod. |
| `eks-staging` | staging-only write | `wake`/`sleep`/`restart`/`status`/`logs`, pinned to one staging namespace fixed at deploy time. |
| `github` | scoped write | Branch/PR/merge-staging/checks; merging `main` is impossible. |
| `cloudwatch` | read-only | Logs + metrics + alarms. |
| `linear-server` | scoped write | Read issues, comment on the claimed issue. |

## 3. Why this is safe — the boundary is the design

The permission model is enforced at **three independent layers**, never the prompt. Prompt rules are
advisory; these are guarantees:

1. **Cloud IAM.** A dedicated role, deny-by-default: a thin `Allow` for read-only observability +
   scoped staging EKS, plus an explicit **`Deny` on every production resource** (prod RDS mutation,
   prod secrets, EKS control-plane mutation). Explicit Deny beats any Allow, so production is
   unreachable at the credential layer. See [`infra/iam-agent.tf`](infra/iam-agent.tf).
2. **Version control.** Branch protection on `main` requires human review and excludes the agent App
   identity; the App also lacks the ability to dispatch the production deploy workflow.
3. **Capability (MCP).** The tool allowlist contains only scoped tools; the dangerous capabilities
   (prod write, merge `main`, prod deploy) **do not exist** in the agent's environment.

The key consequence: **there is no per-action approval to click.** The earlier "approval broker"
design is unnecessary because there is no dangerous write left to gate — staging is disposable and
freely writable, production is read-only, and anything beyond that line is a *human action*, not an
approval. This is strictly safer than runtime approval (nothing to mis-click or socially engineer)
and far less to build.

A change is a permission **bug** if it: adds a broad cloud credential to the agent environment, adds
the agent identity to `main`'s allowed-merge list, or grants the agent raw `aws`/`kubectl`/`psql`/`gh`.

### Precondition: sandbox staging outbound
"Free staging writes" is only safe if a staging deploy/test **cannot emit to real users.** Before
enabling staging writes that can send, confirm staging secrets point outbound providers (email, chat,
webhooks, exports) at test endpoints — a sandbox key, a test workspace, staging sinks. Until an
integration is sandboxed, the agent may write the staging DB but must not trigger staging *sends*.

## 4. Why issues are never lost

1. **Status = durable queue.** Crash mid-run → the issue still sits in In Progress, visible.
2. **Lease + sweeper.** Idle past the timeout → swept back to a human queue (AI Blocked).
3. **Bounded retries → explicit escalation.** Never infinite-retry; after repeated failure → AI
   Blocked with a reason comment. The human queue is never a black hole.
4. **Every transition writes a comment** — a human-readable reconstruction of what ran.
5. **Idempotent claim.** The compare-and-set status move prevents a double-poll from forking work.

## 5. Components

| Path | Role |
|---|---|
| `dispatcher/` | The loop: poll, claim (CAS), lease-sweep, spawn worker, land status, email. Stdlib only. |
| `worker-prompt.md` | The per-issue methodology, templated with the ticket/title/branch. |
| `mcp/agent_rds_mcp.py` | DB tool — prod read-only, staging read/write. |
| `mcp/eks_staging_mcp.py` + `eks_staging.sh` | Staging-only Kubernetes ops (namespace-locked). |
| `mcp/github_mcp.py` | Scoped git via the agent App (PR to main, merge staging only). |
| `agent.mcp.json` | The worker's entire tool surface — only scoped servers, no raw CLI. |
| `infra/iam-agent.tf` | The scoped cloud role — explicit prod-deny (Layer 1). |
| `systemd/agent-dispatcher.service` | Runs the dispatcher as a service. |

## 6. Suggested rollout

Bring it up in phases rather than all at once:

1. **Foundations.** Create the two statuses, the agent tracker identity, the git App, and the scoped
   cloud role with prod-deny. Run the methodology by hand first to trust it.
2. **Polling dispatcher, concurrency 1.** Poll AI Ready → claim → run → AI Review. Push branches
   only; heavy logging. Prove claim/lease/escalation on a few issues.
3. **Independent auditor + staging writes + emails.** Add a read-only auditor gate, enable the
   staging read/write + staging-deploy tools, and the terminal-state email. **Precondition: staging
   outbound sandboxed.**
4. **Trigger + auto-discovery, graduated autonomy.** Add a webhook for latency, wire alerts to file
   issues into Backlog (humans still promote to AI Ready), and raise concurrency as confidence grows.
   The human exit gate and the production barrier never move.
