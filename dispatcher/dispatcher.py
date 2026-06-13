"""Agent-fleet dispatcher — the single long-running loop.

Each tick: sweep stale AI Processing, resume awaiting-input issues, then claim new AI Ready issues.
Per issue a worker runs; on `paused` the dispatcher auto-continues (bounded by MAX_CONTINUATIONS),
parks external/human waits in AI Awaiting Input, and handles `decomposed` epics. Continuation +
awaiting state is persisted (state.py) so a restart recovers in-flight work. The issue status is the
durable queue. See DESIGN.md and docs/long-running-tasks.md.
"""
import datetime as dt
import threading
import time

from . import config, linear_api, notify, runner, state


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).isoformat(), *a, flush=True)


_active = {}     # issue_id -> thread (running, including across more_runtime continuations)
_cont = {}       # issue_id -> {"continuations","session_id","human_reply","issue"}
_awaiting = {}   # issue_id -> {"reason","branch","issue","recheck_at"(epoch|None),"comment_count"}
_lock = threading.Lock()


TAG = "🤖 **AI AGENT** —"  # prefix so every post is unmistakably from the autonomous AI, not a person


def _claim_comment(run_id):
    return (f"{TAG} claimed by the agent fleet (run `{run_id}`). I'm an autonomous AI working this in an "
            f"isolated worktree off latest `main`. I'll move it to **AI Review** on success, or "
            f"**AI Awaiting Input** if I get stuck or need your input.")


def _set_cont(issue, cont):
    cont["issue"] = issue
    _cont[issue["id"]] = cont
    state.save_cont(issue["id"], cont)


def _clear_cont(issue_id):
    _cont.pop(issue_id, None)
    state.del_cont(issue_id)


def _finish_success(issue, result):
    linear_api.update_state(issue["id"], config.STATUS_AI_REVIEW)
    notify.review_ready(issue, result)
    log(issue["identifier"], "→ AI Review:", result.get("summary", ""))


def _finish_blocked(issue, result):
    linear_api.update_state(issue["id"], config.STATUS_AI_AWAITING_INPUT)
    linear_api.comment(issue["id"],
                       f"{TAG} ⛔ **Blocked.** {result.get('blocked_reason', '')}\n\n{result.get('summary', '')}")
    notify.blocked(issue, result)
    log(issue["identifier"], "→ AI Awaiting Input:", result.get("blocked_reason", ""))


def _handle_decomposed(issue, result):
    subs = result.get("subtasks") or []
    linear_api.comment(issue["id"],
                       "🧩 **AI AGENT decomposed this into sub-tickets.** The foundational piece is in the PR; the rest "
                       "are tracked as: " + (", ".join(subs) if subs else "(see comments)") +
                       f"\n\n{result.get('summary', '')}")
    linear_api.update_state(issue["id"], config.STATUS_AI_REVIEW)
    notify.review_ready(issue, result)
    log(issue["identifier"], "→ AI Review (decomposed):", subs)


def _park_awaiting(issue, branch, result):
    reason = result.get("pause_reason", "waiting_external")
    linear_api.update_state(issue["id"], config.STATUS_AI_AWAITING_INPUT)
    comment_count = 0
    recheck = None
    if reason == "needs_human":
        q = result.get("question") or "I need a decision to proceed."
        linear_api.comment(issue["id"], f"{TAG} ⏸️ **Awaiting your input.** {q}\n\n_Reply here and I'll continue._")
        try:
            comment_count = len(linear_api.comments(issue["id"]))
        except Exception:
            comment_count = 0
    else:  # waiting_external
        recheck = time.time() + config.EXTERNAL_RECHECK_SEC
        linear_api.comment(issue["id"],
                           f"{TAG} ⏸️ Waiting on `{result.get('wait_for', 'external')}` — I'll re-check shortly.")
    rec = {"reason": reason, "branch": branch, "issue": issue,
           "recheck_at": recheck, "comment_count": comment_count}
    _awaiting[issue["id"]] = rec
    state.save_awaiting(issue["id"], rec)
    log(issue["identifier"], "→ AI Awaiting Input:", reason)


def work_issue(issue):
    ident = issue["identifier"]
    branch = issue.get("branchName") or runner._fallback_branch(issue)
    issue["branchName"] = branch
    try:
        while True:
            cont = _cont.get(issue["id"], {"continuations": 0, "session_id": None})
            tag = f"(cont {cont['continuations']})" if cont["continuations"] else ""
            log(ident, "→ running worker", tag, "branch", branch)
            result = runner.run_worker(issue, branch, cont)
            status = result.get("status", "blocked")

            if status == "success":
                _finish_success(issue, result)
                break
            if status == "decomposed":
                _handle_decomposed(issue, result)
                break
            if status == "paused":
                cont["continuations"] += 1
                cont["session_id"] = result.get("session_id") or cont.get("session_id")
                cont.pop("human_reply", None)
                _set_cont(issue, cont)
                if cont["continuations"] > config.MAX_CONTINUATIONS:
                    _finish_blocked(issue, {
                        "blocked_reason": f"continuation budget ({config.MAX_CONTINUATIONS}) exhausted — "
                                          "needs decomposition or human help",
                        "summary": result.get("summary", "")})
                    break
                if result.get("pause_reason", "more_runtime") == "more_runtime":
                    log(ident, "paused → auto-continuing", cont["continuations"])
                    continue  # same thread, same concurrency slot
                _park_awaiting(issue, branch, result)
                break  # external/human wait → resumed by process_awaiting()
            # default: blocked
            _finish_blocked(issue, result)
            break
    except Exception as e:
        log(ident, "ERROR", repr(e))
        try:
            _finish_blocked(issue, {"blocked_reason": f"dispatcher error: {e!r}", "summary": ""})
        except Exception:
            pass
    finally:
        # keep continuation state + worktree while parked in awaiting; clear on terminal outcomes
        if issue["id"] not in _awaiting:
            _clear_cont(issue["id"])
            if not config.KEEP_WORKTREES:
                try:
                    runner.remove_worktree(branch)
                except Exception:
                    pass
        with _lock:
            _active.pop(issue["id"], None)


def _spawn(issue):
    t = threading.Thread(target=work_issue, args=(issue,), daemon=True)
    with _lock:
        _active[issue["id"]] = t
    t.start()


def sweep():
    """AI Processing + assigned-to-agent + idle past lease → AI Awaiting Input (a human re-queues it)."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=config.LEASE_MINUTES)
    for iss in linear_api.list_by_status(config.STATUS_AI_PROCESSING):
        if not iss.get("assignee") or iss["assignee"]["id"] != config.AGENT_USER_ID:
            continue
        with _lock:
            if iss["id"] in _active:
                continue
        try:
            updated = dt.datetime.fromisoformat(iss["updatedAt"].replace("Z", "+00:00"))
        except Exception:
            continue
        if updated < cutoff:
            log(iss["identifier"], "lease expired → AI Awaiting Input")
            linear_api.update_state(iss["id"], config.STATUS_AI_AWAITING_INPUT)
            linear_api.comment(iss["id"],
                               f"{TAG} ⛔ Lease expired (idle > {config.LEASE_MINUTES}m). Re-queue to **AI Ready** to retry.")
            _clear_cont(iss["id"])


def process_awaiting():
    """Resume issues parked on an external wait (recheck timer) or a human reply (new comment)."""
    for iid, info in list(_awaiting.items()):
        with _lock:
            if iid in _active or len(_active) >= config.CONCURRENCY:
                continue
        ready = False
        if info["reason"] == "waiting_external":
            ready = bool(info.get("recheck_at")) and time.time() >= info["recheck_at"]
        elif info["reason"] == "needs_human":
            try:
                reply = linear_api.new_reply_since(iid, info.get("comment_count", 0))
            except Exception:
                reply = None
            if reply:
                cont = _cont.get(iid, {"continuations": 1, "session_id": None})
                cont["human_reply"] = reply
                _set_cont(info["issue"], cont)
                ready = True
        if ready:
            _awaiting.pop(iid, None)
            state.del_awaiting(iid)
            iss = info["issue"]
            linear_api.update_state(iss["id"], config.STATUS_AI_PROCESSING)
            log(iss["identifier"], "resuming from awaiting:", info["reason"])
            _spawn(iss)


def tick():
    sweep()
    process_awaiting()
    with _lock:
        if config.CONCURRENCY - len(_active) <= 0:
            return
    for iss in linear_api.list_by_status(config.STATUS_AI_READY):
        with _lock:
            if len(_active) >= config.CONCURRENCY:
                break
            if iss["id"] in _active:
                continue
        claimed = linear_api.try_claim(iss["id"])
        if not claimed:
            continue
        run_id = f"{iss['identifier']}-{int(time.time())}"
        linear_api.comment(iss["id"], _claim_comment(run_id))
        log(iss["identifier"], "claimed", run_id)
        _spawn(claimed)


def recover():
    """On startup, reload persisted state and re-spawn continuations interrupted by a stop/restart."""
    cont, awaiting = state.load()
    _cont.update(cont)
    _awaiting.update(awaiting)
    if cont or awaiting:
        log("recovered state:", len(cont), "continuation(s),", len(awaiting), "awaiting")
    # Re-spawn interrupted AI Processing continuations (their worker died on shutdown). Awaiting issues
    # resume on their own via process_awaiting().
    for iid, c in list(_cont.items()):
        if iid in _awaiting:
            continue
        iss = c.get("issue")
        if not iss:
            continue
        with _lock:
            if len(_active) >= config.CONCURRENCY:
                break
        log(iss.get("identifier"), "recovering interrupted continuation", c.get("continuations"))
        _spawn(iss)


def main():
    config.require("LINEAR_API_KEY", "TEAM_KEY", "AGENT_USER_ID",
                   "STATUS_AI_READY", "STATUS_AI_PROCESSING", "STATUS_AI_REVIEW", "STATUS_AI_AWAITING_INPUT")
    log("dispatcher up — team", config.TEAM_KEY, "concurrency", config.CONCURRENCY,
        "poll", config.POLL_INTERVAL_SEC, "lease", config.LEASE_MINUTES, "m max-cont", config.MAX_CONTINUATIONS)
    recover()
    while True:
        try:
            tick()
        except Exception as e:
            log("tick error:", repr(e))
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
