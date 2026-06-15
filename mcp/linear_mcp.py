#!/usr/bin/env python3
"""Stdio MCP server for scoped, TOKEN-authenticated Linear access (agent worker).

Why this exists: the hosted Linear MCP (mcp.linear.app) is OAuth-only and cannot authenticate in a
headless worker — so workers ran blind (title only), couldn't read the description/acceptance criteria,
and couldn't post their preliminary/completion comments. This server talks to the Linear GraphQL API
with a Personal API key (LINEAR_API_KEY), exactly like the dispatcher does, so it ALWAYS works
headless. No browser, no OAuth, nothing to expire mid-run.

Scoped to what the worker legitimately needs:
  - get_issue        read an issue WITH its description + comments + labels (the thing that was missing)
  - add_comment      post a comment (preliminary review / completion report)
  - create_sub_issue file a linked sub-issue (decomposition / cross-repo follow-ups)

It deliberately does NOT change issue status — the dispatcher owns status transitions (so a worker
can't fight the queue). The key is read from the server's env / a 0600 file, never placed in a tool
argument or returned to the model. Zero external deps: stdlib + the Linear GraphQL endpoint.

Config (env, see example.env):
  LINEAR_API_KEY    the Personal API key (preferred), OR
  LINEAR_KEY_FILE   path to an env file containing `LINEAR_API_KEY=...` (fallback; default
                    ~/.config/agent-fleet/linear.env). Keeps the key off the rendered config + argv.
"""
import json
import os
import re
import sys
import urllib.request

API = "https://api.linear.app/graphql"


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _key():
    k = os.environ.get("LINEAR_API_KEY", "").strip()
    if k:
        return k
    f = os.environ.get("LINEAR_KEY_FILE", os.path.expanduser("~/.config/agent-fleet/linear.env"))
    if os.path.exists(f):
        for line in open(f):
            line = line.strip()
            if line.startswith("LINEAR_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _gql(query, variables=None):
    key = _key()
    if not key:
        raise RuntimeError("LINEAR_API_KEY unavailable (set LINEAR_API_KEY or LINEAR_KEY_FILE)")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": key},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if data.get("errors"):
        raise RuntimeError(f"linear graphql error: {data['errors']}")
    return data["data"]


_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def _resolve(idstr):
    """Accept a UUID or a human identifier (e.g. CLU-123) → (issue_uuid, team_uuid)."""
    idstr = (idstr or "").strip()
    if _IDENT.match(idstr):
        team_key, num = idstr.upper().split("-")
        d = _gql("query($k:String!,$n:Float){issues(filter:{team:{key:{eq:$k}},"
                 "number:{eq:$n}},first:1){nodes{id team{id}}}}", {"k": team_key, "n": float(num)})
        nodes = d["issues"]["nodes"]
        if not nodes:
            raise RuntimeError(f"issue '{idstr}' not found")
        return nodes[0]["id"], nodes[0]["team"]["id"]
    d = _gql("query($id:String!){issue(id:$id){id team{id}}}", {"id": idstr})
    return d["issue"]["id"], d["issue"]["team"]["id"]


def _get_issue(args):
    uid, _ = _resolve(args.get("id", ""))
    d = _gql("query($id:String!){issue(id:$id){identifier title description url "
             "state{name} labels{nodes{name}} "
             "comments{nodes{body createdAt user{displayName}}}}}", {"id": uid})
    return json.dumps(d["issue"], indent=2, ensure_ascii=False)


def _add_comment(args):
    body = args.get("body", "")
    if not body.strip():
        return "refusing to post an empty comment"
    uid, _ = _resolve(args.get("id", ""))
    d = _gql("mutation($i:String!,$b:String!){commentCreate(input:{issueId:$i,body:$b}){success}}",
             {"i": uid, "b": body})
    return "comment posted" if d["commentCreate"]["success"] else "comment failed"


def _create_sub_issue(args):
    parent = args.get("parent", "")
    title = args.get("title", "")
    if not parent or not title:
        return "create_sub_issue requires parent and title"
    puid, team = _resolve(parent)
    d = _gql("mutation($t:String!,$tm:String!,$p:String!,$d:String){"
             "issueCreate(input:{title:$t,teamId:$tm,parentId:$p,description:$d}){"
             "success issue{identifier url}}}",
             {"t": title, "tm": team, "p": puid, "d": args.get("description", "")})
    iss = d["issueCreate"]["issue"]
    return f"created sub-issue {iss['identifier']} — {iss['url']}"


TOOLS = [
    {"name": "get_issue", "fn": _get_issue,
     "description": "Read a Linear issue WITH its description, labels, state, and all comments. "
                    "Use this first to get the full ticket — not just the title. "
                    "id: issue identifier (e.g. CLU-123) or UUID.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "Issue identifier (CLU-123) or UUID."}},
         "required": ["id"]}},
    {"name": "add_comment", "fn": _add_comment,
     "description": "Post a comment on an issue (your preliminary review / completion report). "
                    "Does NOT change status — the dispatcher owns status.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "Issue identifier or UUID."},
         "body": {"type": "string", "description": "Comment markdown."}},
         "required": ["id", "body"]}},
    {"name": "create_sub_issue", "fn": _create_sub_issue,
     "description": "Create a linked sub-issue under a parent (for decomposition or cross-repo "
                    "follow-ups). Inherits the parent's team.",
     "inputSchema": {"type": "object", "properties": {
         "parent": {"type": "string", "description": "Parent issue identifier or UUID."},
         "title": {"type": "string", "description": "Sub-issue title."},
         "description": {"type": "string", "description": "Sub-issue body (markdown)."}},
         "required": ["parent", "title"]}},
]
_BY_NAME = {t["name"]: t["fn"] for t in TOOLS}
_LIST = [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": client_ver, "capabilities": {"tools": {}},
            "serverInfo": {"name": "linear-server", "version": "1.0.0"}}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _LIST}}

    if method == "tools/call":
        params = msg.get("params") or {}
        fn = _BY_NAME.get(params.get("name"))
        if not fn:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        try:
            text, is_err = fn(params.get("arguments") or {}), False
        except Exception as e:
            text, is_err = f"linear error: {e}", True
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}], "isError": is_err}}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    log("linear-server MCP up (token auth); key:", "present" if _key() else "MISSING")
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
