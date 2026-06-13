#!/usr/bin/env python3
"""Agent-hardened stdio MCP server for per-service RDS access.

Two enforced paths, chosen by target (see DESIGN.md):
  - PROD targets  -> a DIRECT read-only connection as a SELECT-only DB user (read-only grants +
    default_transaction_read_only=on). The agent has NO prod-namespace cluster access, so it cannot
    use the pod path for prod — and the read-only user cannot write even if it tried.
  - STAGING-* targets -> a project SQL helper (e.g. an ephemeral psql pod in the staging namespace, r/w).

There is no `rw` parameter and no prod-write path anywhere. Zero external deps: stdlib + psql.
Logs to stderr only.

Deployment config (all env, see example.env):
  APP_REPO              path to the app checkout that holds the staging SQL helper script
  AGENT_RDS_HELPER      relative path of that helper inside APP_REPO (default scripts/rdsql.sh)
  AGENT_DB_TARGETS      comma-separated logical DB/schema names (default "app")
  PROD_RDS_HOST_PATTERN host template for the direct prod read path, "{svc}" -> target name
  PROD_RO_USER          SELECT-only DB user for the prod read path
  PROD_RO_PASSWORD      its password (or supply PROD_RO_ENV_FILE pointing at a 0600 env file)
  AGENT_TENANT_HINT     optional: a tenant/account id surfaced in the tool description
"""
import json
import os
import subprocess
import sys

# Reuse the app repo's SQL helper for the STAGING path (one source of truth for that plumbing).
# This repo is standalone, so point at the app checkout via APP_REPO.
APP_REPO = os.environ.get("APP_REPO", os.path.expanduser("~/app"))
SCRIPT = os.path.join(APP_REPO, os.environ.get("AGENT_RDS_HELPER", "scripts/rdsql.sh"))

# Logical DB/schema targets. Each becomes a prod (read-only) and a staging-<name> (read/write) target.
PROD = [t.strip() for t in os.environ.get("AGENT_DB_TARGETS", "app").split(",") if t.strip()]
STAGING = ["staging-" + s for s in PROD]
TARGETS = PROD + STAGING

# Prod read-only connection. The agent reads prod directly as a SELECT-only user; host per service,
# single `postgres` db, schema chosen by search_path. Password from env or a 0600 file (never in the
# repo / mcp.json). "{svc}" in the pattern is replaced by the target name.
PROD_HOST = os.environ.get("PROD_RDS_HOST_PATTERN", "{svc}.CHANGEME.us-east-1.rds.amazonaws.com")
RO_ENV_FILE = os.environ.get("PROD_RO_ENV_FILE", "/etc/agent/prod-ro.env")


def _ro_creds():
    user = os.environ.get("PROD_RO_USER", "agent_ro")
    pw = os.environ.get("PROD_RO_PASSWORD", "")
    if not pw and os.path.exists(RO_ENV_FILE):
        for line in open(RO_ENV_FILE):
            line = line.strip()
            if line.startswith("PROD_RO_PASSWORD="):
                pw = line.split("=", 1)[1]
            elif line.startswith("PROD_RO_USER="):
                user = line.split("=", 1)[1]
    return user, pw

_TENANT_HINT = os.environ.get("AGENT_TENANT_HINT", "")
TOOL = {
    "name": "query",
    "description": (
        "Run SQL against a per-service RDS. "
        "PROD targets (" + ", ".join(PROD) + ") are READ-ONLY — there is no write override. "
        "staging-* targets are read/write (use them to write canary/test rows when verifying a fix). "
        "ALWAYS filter by your tenant/account id in multi-tenant tables."
        + (f" Tenant/account id: {_TENANT_HINT}." if _TENANT_HINT else "")
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": TARGETS,
                       "description": "prod (read-only): " + ", ".join(PROD) + " | staging (r/w): staging-<svc>."},
            "sql": {"type": "string", "description": "SQL to execute (single statement or batch)."},
        },
        "required": ["target", "sql"],
    },
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _run_prod_ro(schema, sql):
    user, pw = _ro_creds()
    if not pw:
        return True, f"prod RO password unavailable (set PROD_RO_PASSWORD or {RO_ENV_FILE})"
    env = dict(os.environ)
    env.update({
        "PGHOST": PROD_HOST.format(svc=schema),
        "PGPORT": "5432",
        "PGUSER": user,
        "PGPASSWORD": pw,
        "PGDATABASE": "postgres",
        "PGSSLMODE": "require",
        # belt-and-suspenders on top of the SELECT-only grants:
        "PGOPTIONS": f"-c search_path={schema} -c default_transaction_read_only=on",
    })
    try:
        p = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "-c", sql],
                           capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return True, "prod query timed out after 120s"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode != 0, out.strip() or "(no output)"


def run_query(args):
    target = args.get("target", "")
    sql = args.get("sql", "")
    if target not in TARGETS:
        return True, f"unknown target '{target}'. valid: {', '.join(TARGETS)}"
    if not sql.strip():
        return True, "empty sql"
    if target in PROD:
        # Direct read-only connection as the SELECT-only user (no pod, no write path).
        return _run_prod_ro(target, sql)
    # staging-* → staging SQL helper / ephemeral psql pod (read/write). Never pass --rw.
    cmd = ["bash", SCRIPT, target, sql]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return True, "query timed out after 180s (pod scheduling + psql)"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode != 0, out.strip() or "(no output)"


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-rds", "version": "1.0.0"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}

    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "query":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        is_err, text = run_query(params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_err}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    log("agent-rds MCP server up (prod RO, staging RW); script:", SCRIPT)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            log("bad json:", e)
            continue
        try:
            resp = handle(msg)
        except Exception as e:
            log("handler error:", repr(e))
            mid = msg.get("id")
            resp = {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": str(e)}} if mid is not None else None
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
