#!/usr/bin/env python3
"""Minimal stdio MCP server exposing staging-only Kubernetes operations.

Wraps eks_staging.sh, which pins every kubectl call to a single staging namespace (fixed at deploy
time). The prod namespace is never addressable from this tool — that boundary lives in the shell
layer, not the prompt (see DESIGN.md).

Zero external deps: newline-delimited JSON-RPC 2.0 over stdin/stdout. Logs to stderr only.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "eks_staging.sh")

TOOL = {
    "name": "eks_staging",
    "description": (
        "Operate the staging Kubernetes namespace (staging ONLY — prod is unreachable). "
        "Use to bring staging online before testing a change, restart a service after deploy, "
        "or read a staging service's logs. Actions: "
        "status (list deployments); wake (scale the configured services to 1 — wake before testing "
        "if staging is scaled to zero); sleep (scale all to 0); restart <deployment>; "
        "logs <deployment> [lines]."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "wake", "sleep", "restart", "logs"]},
            "deployment": {"type": "string",
                           "description": "Deployment name (required for restart/logs), e.g. gateway-service."},
            "lines": {"type": "integer", "description": "Log tail length for action=logs (default 200)."},
        },
        "required": ["action"],
    },
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run_action(args):
    action = args.get("action", "")
    deployment = args.get("deployment", "")
    lines = args.get("lines")
    cmd = ["bash", SCRIPT, action]
    if action in ("restart", "logs"):
        if not deployment:
            return True, f"action '{action}' requires a deployment name"
        cmd.append(deployment)
        if action == "logs" and lines:
            cmd.append(str(int(lines)))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return True, "eks_staging timed out after 180s"
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
            "serverInfo": {"name": "eks-staging", "version": "1.0.0"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}

    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "eks_staging":
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        is_err, text = run_action(params.get("arguments") or {})
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_err}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    log("eks-staging MCP server up; script:", SCRIPT)
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
