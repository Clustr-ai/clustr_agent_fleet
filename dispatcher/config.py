"""Dispatcher configuration — everything is read from the environment.

No site-specific identifiers are baked in. Defaults are generic placeholders meant to be overridden
at deploy time via an EnvironmentFile (see `example.env`). The Linear status ids and team key are
deployment-specific and have NO usable default — they must be supplied (the dispatcher fails fast at
startup if any are missing). See DESIGN.md for the pattern and README.md for the operator guide.
"""
import os

# The worker runs as a dedicated low-privilege unix user (no SSH key, App-token git, assume-only AWS).
# The dispatcher (this process) holds the Linear/Resend secrets and spawns the worker via
# `sudo -u <RUN_USER>`. The worker operates on ITS OWN app clone so it never touches the
# dispatcher's checkout or credentials. See DESIGN.md.
RUN_USER = os.environ.get("AGENT_RUN_USER", "agent")
RUN_HOME = os.environ.get("AGENT_RUN_HOME", f"/home/{RUN_USER}")
# The application repo clone the worker builds worktrees from (owned by RUN_USER):
REPO = os.environ.get("AGENT_REPO", os.path.join(RUN_HOME, "app"))
WORKTREE_BASE = os.environ.get("AGENT_WORKTREE_BASE", os.path.join(RUN_HOME, "agent-wt"))

# Linear
LINEAR_API_KEY = os.environ.get("LINEAR_API_KEY", "")
LINEAR_API_URL = "https://api.linear.app/graphql"
TEAM_KEY = os.environ.get("LINEAR_TEAM_KEY", "")  # e.g. "ENG" — your Linear team key
AGENT_USER_ID = os.environ.get("AGENT_LINEAR_USER_ID", "")  # the agent's Linear identity (user id)

# Workflow status ids — all deployment-specific, no default. Create dedicated AI states in your Linear
# team workflow so AI work never mixes with human work, then supply each id. The dispatcher refuses to
# start if any are unset. The agent's lifecycle:
#   AI Ready → AI Processing → AI Review (success) | AI Awaiting Input (blocked OR needs input)
# AI Processing replaces the built-in "In Progress" (keep that for humans); AI Awaiting Input is the
# single "agent stopped — a human's turn" state (blocked, a question, or an external wait all land here).
STATUS_AI_READY = os.environ.get("STATUS_AI_READY", "")
STATUS_AI_PROCESSING = os.environ.get("STATUS_AI_PROCESSING", "")   # AI work only (NOT human In Progress)
STATUS_AI_REVIEW = os.environ.get("STATUS_AI_REVIEW", "")
STATUS_AI_AWAITING_INPUT = os.environ.get("STATUS_AI_AWAITING_INPUT", "")  # blocked + needs-input merged

# Loop / concurrency
POLL_INTERVAL_SEC = int(os.environ.get("AGENT_POLL_INTERVAL_SEC", "60"))
CONCURRENCY = int(os.environ.get("AGENT_CONCURRENCY", "1"))  # start at 1; raise once trusted
LEASE_MINUTES = int(os.environ.get("AGENT_LEASE_MINUTES", "30"))  # In Progress idle > this → swept

# Long-running tasks / continuation (docs/long-running-tasks.md)
MAX_CONTINUATIONS = int(os.environ.get("AGENT_MAX_CONTINUATIONS", "6"))  # bound the auto-continue loop
USE_RESUME = os.environ.get("AGENT_USE_RESUME", "0") == "1"  # claude --resume fast path (else rehydrate)
EXTERNAL_RECHECK_SEC = int(os.environ.get("AGENT_EXTERNAL_RECHECK_SEC", "300"))  # waiting_external poll

# Worker run (paths are in RUN_USER's home — its own agent-fleet checkout)
FLEET_REPO = os.environ.get("AGENT_FLEET_REPO", os.path.join(RUN_HOME, "agent_fleet"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", os.path.join(RUN_HOME, ".npm-global/bin/claude"))
CLAUDE_MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
MCP_CONFIG = os.environ.get("AGENT_MCP_CONFIG", os.path.join(FLEET_REPO, "agent.mcp.json"))
# worker-prompt is read by the dispatcher from ITS OWN fleet checkout to build the prompt:
WORKER_PROMPT = os.environ.get("AGENT_WORKER_PROMPT",
                               os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker-prompt.md"))
WORKER_TIMEOUT_SEC = int(os.environ.get("AGENT_WORKER_TIMEOUT_SEC", "3600"))
KEEP_WORKTREES = os.environ.get("AGENT_KEEP_WORKTREES", "1") == "1"

# Notifications (Resend)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL = os.environ.get("AGENT_NOTIFY_EMAIL", "")  # who gets terminal-state emails
NOTIFY_FROM = os.environ.get("AGENT_NOTIFY_FROM", "Agent <agent@example.com>")  # verified Resend domain


def require(*names):
    """Fail fast at startup if a required env var is unset."""
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise SystemExit(f"missing required config: {', '.join(missing)}")
