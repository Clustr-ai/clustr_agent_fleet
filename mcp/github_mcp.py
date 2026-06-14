#!/usr/bin/env python3
"""Stdio MCP server for scoped GitHub actions (agent pipeline).

Wraps the `gh` CLI authenticated as a dedicated agent GitHub App (token in this server's env, never
in the agent context). Capabilities are deliberately narrow (see DESIGN.md, Layer 2):

  - pr_create     open a PR (default base `staging`) for the HUMAN review gate
  - pr_checks     read CI / check-run status for a branch or PR
  - merge_staging merge a branch into the staging branch (triggers the staging deploy) — base is
                  HARD-PINNED to the staging branch; merging into `main`/prod is impossible here

The `main` barrier is enforced twice: here (no tool can target main) and by branch protection on the
GitHub side (the App identity is not in main's allowed-merge set). Zero external deps beyond `gh`.

Config (env, see example.env):
  GH_REPO                 "owner/repo" the agent operates on (falls back to cwd repo if empty)
  GH_STAGING_BRANCH       the deploy-on-merge staging branch (default "staging")
  GH_APP_ID               agent GitHub App id
  GH_APP_INSTALLATION_ID  its installation id on the repo
  GH_APP_KEY_FILE         path to the App private key (.pem), 0600
  GH_TOKEN                fallback explicit token (e.g. PAT) if the App is not configured
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = os.environ.get("GH_REPO", "")  # e.g. "your-org/your-app"; falls back to cwd repo if empty
STAGING_BRANCH = os.environ.get("GH_STAGING_BRANCH", "staging")
# Base branch for review PRs. Defaults to the staging branch so the agent's PRs target staging (deploy
# on merge), not main directly. Override with GH_PR_BASE for a different review target.
PR_BASE = os.environ.get("GH_PR_BASE", STAGING_BRANCH)

# GitHub App auth (the agent's distinct, non-allowlisted identity — see DESIGN.md, Layer 2).
# Installation tokens are short-lived; we mint + cache them from the App private key. The App is NOT
# in main's push allowlist, so even this token cannot write main — GitHub enforces it.
GH_APP_ID = os.environ.get("GH_APP_ID", "")
GH_APP_INSTALLATION_ID = os.environ.get("GH_APP_INSTALLATION_ID", "")
GH_APP_KEY_FILE = os.environ.get("GH_APP_KEY_FILE", "/etc/agent/gh-app-key.pem")
_token_cache = {"token": "", "exp": 0}


def _b64url(b):
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _mint_jwt():
    now = int(time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": GH_APP_ID}).encode())
    signing_input = f"{header}.{payload}".encode()
    # Sign with openssl (no crypto lib dependency).
    p = subprocess.run(["openssl", "dgst", "-sha256", "-sign", GH_APP_KEY_FILE],
                       input=signing_input, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"jwt sign failed: {p.stderr.decode()[:200]}")
    return f"{header}.{payload}.{_b64url(p.stdout)}"


def _installation_token():
    """Return a cached installation token, minting a fresh one when it's within 5 min of expiry."""
    if not (GH_APP_ID and GH_APP_INSTALLATION_ID):
        return os.environ.get("GH_TOKEN", "")  # fallback: explicit token (e.g. PAT) if App not configured
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 300:
        return _token_cache["token"]
    jwt = _mint_jwt()
    req = urllib.request.Request(
        f"https://api.github.com/app/installations/{GH_APP_INSTALLATION_ID}/access_tokens",
        method="POST",
        headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    _token_cache["token"] = data["token"]
    # tokens last 1h; cache for ~55m
    _token_cache["exp"] = time.time() + 55 * 60
    return _token_cache["token"]

TOOL = {
    "name": "github",
    "description": (
        "Scoped GitHub actions as the dedicated agent App. Actions: "
        "pr_create (open a PR to base 'staging' for human review — head, title, body); "
        "pr_checks (CI status for a branch/PR — ref); "
        "merge_staging (merge a branch into the staging branch to deploy + test on staging — head). "
        "Merging into main/prod is NOT possible from this tool."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["pr_create", "pr_checks", "merge_staging"]},
            "head": {"type": "string", "description": "Source branch (pr_create / merge_staging)."},
            "ref": {"type": "string", "description": "Branch or PR number (pr_checks)."},
            "title": {"type": "string", "description": "PR title (pr_create)."},
            "body": {"type": "string", "description": "PR body (pr_create)."},
        },
        "required": ["action"],
    },
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def gh(*gh_args):
    cmd = ["gh", *gh_args]
    if REPO:
        cmd += ["--repo", REPO]
    env = dict(os.environ)
    try:
        env["GH_TOKEN"] = _installation_token()  # authenticate gh as the App installation
    except Exception as e:
        return 1, f"could not mint GitHub App token: {e}"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return 1, "gh timed out after 120s"
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def run_action(args):
    action = args.get("action", "")

    if action == "pr_create":
        head = args.get("head", "")
        if not head:
            return True, "pr_create requires head"
        title = args.get("title") or f"{head}"
        body = args.get("body") or ""
        rc, out = gh("pr", "create", "--base", PR_BASE, "--head", head,
                     "--title", title, "--body", body)
        return rc != 0, out or "(pr created)"

    if action == "pr_checks":
        ref = args.get("ref", "")
        if not ref:
            return True, "pr_checks requires ref"
        rc, out = gh("pr", "checks", ref)
        return rc != 0, out or "(no checks)"

    if action == "merge_staging":
        head = args.get("head", "")
        if not head:
            return True, "merge_staging requires head"
        # base is HARD-PINNED to staging — main is never a target here.
        rc, out = gh("pr", "create", "--base", STAGING_BRANCH, "--head", head,
                     "--title", f"[staging] {head}", "--body", "Auto-merge to staging for testing.")
        # If a PR already exists, gh prints it; proceed to merge by head branch either way.
        rc2, out2 = gh("pr", "merge", head, "--merge", "--delete-branch=false")
        ok = rc2 == 0
        return (not ok), f"create: {out}\nmerge: {out2}"

    return True, f"unknown action '{action}'"


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "github", "version": "1.0.0"},
        }}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}

    if method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != "github":
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
    log("github MCP server up; repo:", REPO or "(cwd)")
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
