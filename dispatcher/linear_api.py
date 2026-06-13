"""Minimal Linear GraphQL client (stdlib only).

Used by the dispatcher to drive the issue queue: list `AI Ready`, claim (→ In Progress + assign),
comment, transition to AI Review / AI Blocked, and find stale In-Progress issues for the sweeper.
The issue status IS the durable queue (DESIGN.md §4) — this client just moves rows.
"""
import json
import urllib.request

from . import config


def _gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        config.LINEAR_API_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": config.LINEAR_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if "errors" in data:
        raise RuntimeError(f"linear graphql error: {data['errors']}")
    return data["data"]


_ISSUE_FIELDS = "id identifier title branchName updatedAt state { id name } assignee { id }"


def list_by_status(state_id):
    q = """
    query($team:String!,$state:ID!){
      issues(filter:{ team:{ key:{ eq:$team } }, state:{ id:{ eq:$state } } }, first:50){
        nodes { %s }
      }
    }""" % _ISSUE_FIELDS
    return _gql(q, {"team": config.TEAM_KEY, "state": state_id})["issues"]["nodes"]


def get_issue(issue_id):
    q = "query($id:String!){ issue(id:$id){ %s description } }" % _ISSUE_FIELDS
    return _gql(q, {"id": issue_id})["issue"]


def update_state(issue_id, state_id, assignee_id=None):
    inp = {"stateId": state_id}
    if assignee_id:
        inp["assigneeId"] = assignee_id
    q = """
    mutation($id:String!,$input:IssueUpdateInput!){
      issueUpdate(id:$id, input:$input){ success }
    }"""
    return _gql(q, {"id": issue_id, "input": inp})["issueUpdate"]["success"]


def comment(issue_id, body):
    q = """
    mutation($input:CommentCreateInput!){ commentCreate(input:$input){ success } }"""
    return _gql(q, {"input": {"issueId": issue_id, "body": body}})["commentCreate"]["success"]


def comments(issue_id):
    """Return the issue's comments oldest→newest: [{body, user_id}]."""
    q = "query($id:String!){ issue(id:$id){ comments{ nodes{ body createdAt user{ id } } } } }"
    nodes = _gql(q, {"id": issue_id})["issue"]["comments"]["nodes"]
    return [{"body": n["body"], "user_id": (n.get("user") or {}).get("id")} for n in nodes]


def new_reply_since(issue_id, since_count):
    """Return the newest comment body if any was added beyond `since_count`, else None.

    Used to resume a `needs_human` pause: while the issue is parked in AI Awaiting Input the worker has
    exited, so *any* new comment is an external (human) reply — no author comparison needed, which means
    this works even when the agent shares the human's Linear identity (V1). Prefer a non-agent author
    when one is distinguishable, but fall back to the newest comment."""
    cs = comments(issue_id)
    if len(cs) <= since_count:
        return None
    return cs[-1]["body"]


def try_claim(issue_id):
    """CAS-ish claim: re-fetch; only transition if still AI Ready and unassigned. Returns the issue
    dict on success, else None (someone else grabbed it)."""
    iss = get_issue(issue_id)
    if iss["state"]["id"] != config.STATUS_AI_READY:
        return None
    if iss.get("assignee"):
        return None
    update_state(issue_id, config.STATUS_IN_PROGRESS, config.AGENT_USER_ID)
    return iss
