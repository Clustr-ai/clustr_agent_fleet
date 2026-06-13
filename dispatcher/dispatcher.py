"""Agent-pipeline dispatcher — the single long-running loop (DESIGN.md §2).

Each tick:
  1. Sweep: In-Progress issues assigned to the agent, idle past the lease → AI Blocked (recoverable).
  2. Claim: up to (CONCURRENCY - active) issues in `AI Ready` → In Progress + assign + comment.
  3. Work: one headless worker per claimed issue, in its own worktree, in a thread.
  4. Land: parse the worker's RESULT → move status to AI Review (success) or AI Blocked → email.

Linear status is the durable queue, so a crash loses nothing: the issue stays where it was and the
sweeper or the next claim picks it up. Run via systemd (see systemd/agent-dispatcher.service).
"""
import datetime as dt
import threading
import time

from . import config, linear_api, notify, runner


def log(*a):
    print(dt.datetime.utcnow().isoformat(), *a, flush=True)


_active = {}  # issue_id -> thread
_lock = threading.Lock()


def _claim_comment(run_id):
    return (f"🤖 Claimed by the agent pipeline — run `{run_id}`. "
            f"Working in an isolated worktree off latest `main`. "
            f"Status will move to **AI Review** on success or **AI Blocked** if I get stuck.")


def work_issue(issue):
    ident = issue["identifier"]
    branch = None
    try:
        branch = issue.get("branchName") or runner._fallback_branch(issue)
        issue["branchName"] = branch
        log(ident, "→ running worker as", config.RUN_USER, "branch", branch)
        result = runner.run_worker(issue, branch)
        status = result.get("status", "blocked")
        if status == "success":
            linear_api.update_state(issue["id"], config.STATUS_AI_REVIEW)
            notify.review_ready(issue, result)
            log(ident, "→ AI Review:", result.get("summary", ""))
        else:
            linear_api.update_state(issue["id"], config.STATUS_AI_BLOCKED)
            linear_api.comment(issue["id"],
                               f"⛔ **Blocked.** {result.get('blocked_reason', '')}\n\n{result.get('summary', '')}")
            notify.blocked(issue, result)
            log(ident, "→ AI Blocked:", result.get("blocked_reason", ""))
    except Exception as e:  # never let one issue kill the loop
        log(ident, "ERROR", repr(e))
        try:
            linear_api.update_state(issue["id"], config.STATUS_AI_BLOCKED)
            linear_api.comment(issue["id"], f"⛔ Dispatcher error: `{e!r}`")
        except Exception:
            pass
    finally:
        if branch and not config.KEEP_WORKTREES:
            try:
                runner.remove_worktree(branch)
            except Exception:
                pass
        with _lock:
            _active.pop(issue["id"], None)


def sweep():
    """In-Progress + assigned-to-agent + idle past lease → AI Blocked (a human re-queues it)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=config.LEASE_MINUTES)
    for iss in linear_api.list_by_status(config.STATUS_IN_PROGRESS):
        if not iss.get("assignee") or iss["assignee"]["id"] != config.AGENT_USER_ID:
            continue
        with _lock:
            if iss["id"] in _active:
                continue  # we're actively working it
        try:
            updated = dt.datetime.fromisoformat(iss["updatedAt"].replace("Z", "+00:00"))
        except Exception:
            continue
        if updated < cutoff:
            log(iss["identifier"], "lease expired → AI Blocked")
            linear_api.update_state(iss["id"], config.STATUS_AI_BLOCKED)
            linear_api.comment(iss["id"],
                               f"⛔ Lease expired (idle > {config.LEASE_MINUTES}m). Re-queue to **AI Ready** to retry.")


def tick():
    sweep()
    with _lock:
        free = config.CONCURRENCY - len(_active)
    if free <= 0:
        return
    ready = linear_api.list_by_status(config.STATUS_AI_READY)
    for iss in ready:
        with _lock:
            if len(_active) >= config.CONCURRENCY:
                break
            if iss["id"] in _active:
                continue
        claimed = linear_api.try_claim(iss["id"])
        if not claimed:
            continue  # someone else grabbed it / no longer ready
        run_id = f"{iss['identifier']}-{int(time.time())}"
        linear_api.comment(iss["id"], _claim_comment(run_id))
        t = threading.Thread(target=work_issue, args=(claimed,), daemon=True)
        with _lock:
            _active[iss["id"]] = t
        log(iss["identifier"], "claimed", run_id)
        t.start()


def main():
    config.require("LINEAR_API_KEY", "TEAM_KEY", "AGENT_USER_ID",
                   "STATUS_AI_READY", "STATUS_IN_PROGRESS", "STATUS_AI_REVIEW", "STATUS_AI_BLOCKED")
    log("dispatcher up — team", config.TEAM_KEY, "concurrency", config.CONCURRENCY,
        "poll", config.POLL_INTERVAL_SEC, "lease", config.LEASE_MINUTES, "m")
    while True:
        try:
            tick()
        except Exception as e:
            log("tick error:", repr(e))
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
