#!/usr/bin/env bash
# Poll-based auto-deploy. Run by a systemd timer every couple of minutes: when the fleet repo's
# branch advances, pull BOTH checkouts (the dispatcher's and the worker user's) and restart the
# dispatcher — but ONLY while no worker is running, since a restart kills in-flight workers
# (KillMode=control-group). If a worker is busy, defer to the next tick. No inbound endpoint, no
# CI secrets — same polling philosophy as the dispatcher itself.
#
# Config via env (the systemd unit loads the dispatcher EnvironmentFile, so AGENT_RUN_USER etc. carry):
#   AGENT_DEPLOY_BRANCH        branch to track (default main)
#   AGENT_DISPATCHER_DIR       dispatcher's repo checkout (default /home/ubuntu/agent_fleet)
#   AGENT_RUN_USER             worker user (default agent)
#   AGENT_WORKER_FLEET_DIR     worker user's repo checkout (default /home/<run_user>/agent_fleet)
#   AGENT_SERVICE              systemd service to restart (default agent-dispatcher)
#   APP_REPO                   clustr_app checkout (holds clustr-admin-mcp/ + scripts/); pulled too
set -euo pipefail

BRANCH="${AGENT_DEPLOY_BRANCH:-main}"
DISPATCHER_DIR="${AGENT_DISPATCHER_DIR:-/home/ubuntu/agent_fleet}"
RUN_USER="${AGENT_RUN_USER:-agent}"
WORKER_DIR="${AGENT_WORKER_FLEET_DIR:-/home/$RUN_USER/agent_fleet}"
SERVICE="${AGENT_SERVICE:-agent-dispatcher}"

# The app checkout is part of the deployed surface: agent.mcp.json starts
# $APP_REPO/clustr-admin-mcp/server.mjs, so a stale checkout pins the fleet to an old tool.
# Refreshed on EVERY tick, not only when this repo moves (main moves many times a day here
# and not at all there), and as the worker user, who owns it: ubuntu cannot even see
# /home/<run_user>. No restart needed: each worker run starts the MCP server afresh.
# Best-effort but loud: the symptom downstream is a missing or stale tool.
APP_DIR="${APP_REPO:-/home/$RUN_USER/clustr_app}"
if sudo -u "$RUN_USER" test -d "$APP_DIR/.git"; then
  before=$(sudo -u "$RUN_USER" -H git -C "$APP_DIR" rev-parse --short HEAD)
  if sudo -u "$RUN_USER" -H bash -lc "git -C '$APP_DIR' pull -q --ff-only" 2>/dev/null; then
    after=$(sudo -u "$RUN_USER" -H git -C "$APP_DIR" rev-parse --short HEAD)
    [ "$before" = "$after" ] || echo "$(date -Is) app checkout $before -> $after"
  else
    echo "$(date -Is) WARNING: could not fast-forward $APP_DIR, the fleet runs a STALE clustr-admin-mcp" >&2
  fi
else
  echo "$(date -Is) WARNING: no git checkout at $APP_DIR, clustr-admin-mcp tools are UNAVAILABLE" >&2
fi

# The worker's copy of this repo (agent.mcp.json, prompts, render-mcp-config.py) is read
# afresh by each run, so it also catches up every tick, whether or not the dispatcher moves.
sudo -u "$RUN_USER" -H bash -lc "git -C '$WORKER_DIR' pull -q --ff-only origin '$BRANCH'" 2>/dev/null   || echo "$(date -Is) WARNING: could not fast-forward $WORKER_DIR, workers run a STALE fleet config" >&2

cd "$DISPATCHER_DIR"
git fetch -q origin "$BRANCH"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] && exit 0   # up to date

# A restart would kill in-flight workers — defer until the fleet is idle.
if pgrep -u "$RUN_USER" -f 'claude' >/dev/null 2>&1; then
  echo "$(date -Is) update available but workers active — deferring restart"
  exit 0
fi

echo "$(date -Is) deploying $(git rev-parse --short HEAD) -> $(git rev-parse --short "origin/$BRANCH")"
git pull -q --ff-only origin "$BRANCH"

sudo systemctl restart "$SERVICE"
echo "$(date -Is) deployed + restarted $SERVICE"
