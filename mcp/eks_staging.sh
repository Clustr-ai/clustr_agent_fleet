#!/usr/bin/env bash
# eks_staging.sh — staging-only Kubernetes operations for the agent pipeline.
#
# HARD BOUNDARY: every kubectl call is pinned to a single staging namespace ($STAGING_NAMESPACE,
# fixed per deploy — never caller-supplied per request). The prod namespace is never addressable
# from this script. This is the credentialed half of the `eks-staging` MCP tool (see DESIGN.md) —
# it lets the agent bring staging up to test without raw kubectl.
#
# Config (env, fixed at deploy time — see example.env):
#   STAGING_NAMESPACE  the one namespace this tool may touch (default "staging")
#   EKS_CLUSTER        cluster name for `aws eks update-kubeconfig`
#   AWS_REGION         region (default us-east-1)
#   STAGING_SERVICES   space-separated deployments woken by `wake`
#
# Usage:
#   eks_staging.sh status
#   eks_staging.sh wake                 # scale the configured services 0 -> 1
#   eks_staging.sh sleep                # scale ALL staging deployments -> 0
#   eks_staging.sh restart <deployment> # rollout restart one staging deployment
#   eks_staging.sh logs <deployment> [lines]   # tail logs (default 200 lines)
set -euo pipefail

NS="${STAGING_NAMESPACE:-staging}"      # the single staging namespace — never per-request
CLUSTER="${EKS_CLUSTER:-my-cluster}"
REGION="${AWS_REGION:-us-east-1}"

# The services woken for testing (space-separated). Override via STAGING_SERVICES.
read -r -a ACTIVE_SERVICES <<< "${STAGING_SERVICES:-app}"

ensure_kubeconfig() {
  aws eks update-kubeconfig --name "$CLUSTER" --region "$REGION" >/dev/null 2>&1 || true
}

# Refuse any caller-supplied name that could escape the staging namespace.
safe_name() {
  case "$1" in
    *[!a-z0-9-]*|"") echo "invalid deployment name: '$1'" >&2; exit 2 ;;
  esac
}

CMD="${1:-}"; shift || true
ensure_kubeconfig

case "$CMD" in
  status)
    kubectl get deployments -n "$NS" -o wide
    ;;
  wake)
    kubectl scale -n "$NS" deployment "${ACTIVE_SERVICES[@]}" --replicas=1
    echo "--- waking; current state ---"
    kubectl get deployments -n "$NS"
    ;;
  sleep)
    # Scale every deployment in the namespace to 0 (manual staging-sleeper).
    mapfile -t DEPLOYS < <(kubectl get deployments -n "$NS" -o name)
    [ "${#DEPLOYS[@]}" -eq 0 ] && { echo "no deployments in $NS"; exit 0; }
    kubectl scale -n "$NS" "${DEPLOYS[@]}" --replicas=0
    echo "--- sleeping; current state ---"
    kubectl get deployments -n "$NS"
    ;;
  restart)
    DEP="${1:-}"; safe_name "$DEP"
    kubectl rollout restart -n "$NS" "deployment/$DEP"
    kubectl rollout status -n "$NS" "deployment/$DEP" --timeout=120s
    ;;
  logs)
    DEP="${1:-}"; safe_name "$DEP"
    LINES="${2:-200}"
    case "$LINES" in *[!0-9]*) echo "lines must be numeric" >&2; exit 2 ;; esac
    kubectl logs -n "$NS" "deployment/$DEP" --tail="$LINES" --all-containers=true
    ;;
  *)
    echo "usage: eks_staging.sh {status|wake|sleep|restart <dep>|logs <dep> [lines]}" >&2
    exit 2
    ;;
esac
