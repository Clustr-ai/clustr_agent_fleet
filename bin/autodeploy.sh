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
set -euo pipefail

BRANCH="${AGENT_DEPLOY_BRANCH:-main}"
DISPATCHER_DIR="${AGENT_DISPATCHER_DIR:-/home/ubuntu/agent_fleet}"
RUN_USER="${AGENT_RUN_USER:-agent}"
WORKER_DIR="${AGENT_WORKER_FLEET_DIR:-/home/$RUN_USER/agent_fleet}"
SERVICE="${AGENT_SERVICE:-agent-dispatcher}"

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
sudo -u "$RUN_USER" -H bash -lc "git -C '$WORKER_DIR' pull -q --ff-only origin '$BRANCH'"
sudo systemctl restart "$SERVICE"
echo "$(date -Is) deployed + restarted $SERVICE"
