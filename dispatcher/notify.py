"""Terminal-state notification emails via Resend (stdlib only).

Notification-only — there is nothing to approve (DESIGN.md §3). Sent when a run lands in
AI Review (review ready) or AI Awaiting Input (needs a human). Never raises into the dispatcher loop.
"""
import json
import urllib.request

from . import config


def _send(subject, html):
    if not config.RESEND_API_KEY:
        return  # email is best-effort; missing key shouldn't kill a run
    body = json.dumps({
        "from": config.NOTIFY_FROM,
        "to": [config.NOTIFY_EMAIL],
        "subject": subject,
        "html": html,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.RESEND_API_KEY}"},
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception:
        pass  # swallow — notifications are not load-bearing


def _issue_url(identifier):
    return f"https://linear.app/issue/{identifier}"


def review_ready(issue, result):
    ident = issue["identifier"]
    _send(
        f"✅ {ident} ready for review — {result.get('summary', '')}",
        f"""
        <h2>{ident} — {issue['title']}</h2>
        <p><b>Summary:</b> {result.get('summary', '')}</p>
        <p><b>Branch:</b> {result.get('branch', '')}<br>
           <b>PR:</b> <a href="{result.get('pr_url', '')}">{result.get('pr_url', '')}</a><br>
           <b>Staging deployed:</b> {result.get('staging_deployed', False)}</p>
        <p><a href="{_issue_url(ident)}">Open {ident} in Linear →</a></p>
        <p>Status moved to <b>AI Review</b>. Your move.</p>
        """,
    )


def blocked(issue, result):
    ident = issue["identifier"]
    _send(
        f"⛔ {ident} blocked — needs you",
        f"""
        <h2>{ident} — {issue['title']}</h2>
        <p><b>Blocked reason:</b> {result.get('blocked_reason', '(none given)')}</p>
        <p><b>What it tried:</b> {result.get('summary', '')}</p>
        <p><a href="{_issue_url(ident)}">Open {ident} in Linear →</a></p>
        <p>Status moved to <b>AI Awaiting Input</b>.</p>
        """,
    )
