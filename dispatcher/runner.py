"""Per-issue worker runner.

The worker runs as the low-privilege RUN_USER in a clean login (`sudo -iu`), in its OWN app clone, in
an isolated worktree. Safety is the credential boundary (no SSH key, App-token git that can't push
main, assume-only AWS, SELECT-only prod — DESIGN.md). This module:
  - builds the worker prompt (with continuation context when resuming),
  - renders the MCP config in the worker's env, creates/reuses the worktree (lock-serialized),
  - runs headless Claude with JSON output (so we capture the session id), and
  - parses the RESULT line — with a guarantee path when the worker ends without one.

See docs/long-running-tasks.md for the pause/checkpoint/continue design.
"""
import json
import os
import re
import shlex
import subprocess
import tempfile

from . import config

_RESULT_RE = re.compile(r"^RESULT:\s*(\{.*\})\s*$", re.MULTILINE)


def _as_run_user(script, timeout=None):
    """Run a bash script as RUN_USER in a clean LOGIN env (sources the worker's MCP profile)."""
    fd, sf = tempfile.mkstemp(prefix="agent-run-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(sf, 0o644)
    try:
        return subprocess.run(["sudo", "-iu", config.RUN_USER, "bash", sf],
                              capture_output=True, text=True, timeout=timeout)
    finally:
        try:
            os.unlink(sf)
        except OSError:
            pass


def _fallback_branch(issue):
    slug = re.sub(r"[^a-z0-9]+", "-", issue.get("title", "").lower()).strip("-")[:40]
    return f"agent/{issue['identifier'].lower()}-{slug}"


def build_prompt(issue, cont=None):
    tmpl = open(config.WORKER_PROMPT).read()
    contblock = ""
    if cont and cont.get("continuations", 0) > 0:
        contblock = (
            f"\n## ⏩ CONTINUATION (run #{cont['continuations'] + 1})\n"
            f"You are RESUMING this ticket — your prior work is already in this worktree. FIRST read "
            f"`.agent/{issue['identifier']}.md` (your journal) and run `git diff origin/main` to see "
            f"what's done, then continue from the NEXT unchecked step. Do NOT restart from scratch or "
            f"re-post the preliminary review."
        )
        if cont.get("human_reply"):
            contblock += "\nA human answered your question:\n> " + " ".join(cont["human_reply"].split())
    return (tmpl
            .replace("{{TICKET}}", issue["identifier"])
            .replace("{{TITLE}}", issue.get("title", ""))
            .replace("{{BRANCH}}", issue.get("branchName") or _fallback_branch(issue))
            .replace("{{CONTINUATION}}", contblock))


def _parse_output(out):
    """Extract (result_text, session_id) from `--output-format json` output, robustly.

    A login shell can prepend noise to stdout, so we don't assume the whole stream is the JSON —
    we locate the result object (it starts with `{"type":`) and parse from there.
    """
    s = (out or "").strip()
    candidates = [s]
    i = s.rfind('{"type":')
    if i >= 0:
        candidates.append(s[i:])
    i = s.find("{")
    if i >= 0:
        candidates.append(s[i:])
    for cand in candidates:
        try:
            d = json.loads(cand)
            if isinstance(d, dict) and ("result" in d or "session_id" in d):
                return d.get("result") or "", d.get("session_id")
        except Exception:
            continue
    return out, None  # not JSON — treat as plain text


def _worktree_state(wt, branch):
    """Cheap recovery probe: how much uncommitted work + is the branch pushed."""
    r = _as_run_user(
        f"cd {shlex.quote(wt)} 2>/dev/null && "
        f"echo CHANGED=$(git status --porcelain 2>/dev/null | wc -l) && "
        f"echo PUSHED=$(git ls-remote origin {shlex.quote(branch)} 2>/dev/null | wc -l)", timeout=60)
    changed = pushed = "?"
    for line in (r.stdout or "").splitlines():
        if line.startswith("CHANGED="):
            changed = line.split("=", 1)[1]
        elif line.startswith("PUSHED="):
            pushed = line.split("=", 1)[1]
    return changed, pushed


def run_worker(issue, branch, cont=None):
    """Run (or continue) the worker for one issue. Returns the parsed RESULT dict."""
    prompt = build_prompt(issue, cont)
    safe = branch.replace("/", "-")
    wt = os.path.join(config.WORKTREE_BASE, safe)
    render = os.path.join(config.FLEET_REPO, "bin", "render-mcp-config.py")

    fd, promptfile = tempfile.mkstemp(prefix="agent-prompt-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write(prompt)
    os.chmod(promptfile, 0o644)

    resume = ""
    if cont and cont.get("session_id") and config.USE_RESUME:
        resume = f"--resume {shlex.quote(cont['session_id'])} "
    # JSON output only when we need the session id for --resume; otherwise plain text (robust RESULT
    # parsing, no login-shell-noise fragility). USE_RESUME defaults off.
    outfmt = "--output-format json " if config.USE_RESUME else ""

    lockfile = os.path.join(config.RUN_HOME, ".agent-git.lock")
    script = f"""set -e
mkdir -p {shlex.quote(config.WORKTREE_BASE)}
(
  flock 9
  cd {shlex.quote(config.REPO)}
  git fetch -q origin main
  if git rev-parse --verify {shlex.quote(branch)} >/dev/null 2>&1; then
    git worktree add {shlex.quote(wt)} {shlex.quote(branch)} 2>/dev/null || true
  else
    git worktree add -b {shlex.quote(branch)} {shlex.quote(wt)} origin/main 2>/dev/null || true
  fi
) 9>{shlex.quote(lockfile)}
cd {shlex.quote(wt)}
EXCL="$(git rev-parse --git-path info/exclude)"
grep -qxF '.agent/' "$EXCL" 2>/dev/null || echo '.agent/' >> "$EXCL"
mkdir -p .agent
__MCP="$(mktemp)"
python3 {shlex.quote(render)} {shlex.quote(config.MCP_CONFIG)} > "$__MCP"
{shlex.quote(config.CLAUDE_BIN)} -p "$(cat {shlex.quote(promptfile)})" {resume}\
  --model {shlex.quote(config.CLAUDE_MODEL)} \
  --mcp-config "$__MCP" \
  --strict-mcp-config \
  --permission-mode bypassPermissions \
  {outfmt}--add-dir {shlex.quote(wt)} &
__CPID=$!
# Inactivity watchdog: if Claude writes nothing under ~/.claude for INACTIVITY_SEC, it's hung — kill it
# so the dispatcher recovers now instead of waiting out the full timeout. Healthy runs keep ~/.claude hot.
( while kill -0 $__CPID 2>/dev/null; do
    sleep 60
    if [ -z "$(find $HOME/.claude -type f -newermt @$(( $(date +%s) - {config.INACTIVITY_SEC} )) 2>/dev/null | head -1)" ]; then
      echo "WATCHDOG: no Claude activity for {config.INACTIVITY_SEC}s; killing hung run" >&2
      kill $__CPID 2>/dev/null; sleep 5; kill -9 $__CPID 2>/dev/null; break
    fi
  done ) &
__WPID=$!
wait $__CPID
kill $__WPID 2>/dev/null
rm -f "$__MCP"
"""
    try:
        p = _as_run_user(script, timeout=config.WORKER_TIMEOUT_SEC)
        out = p.stdout or ""
    except subprocess.TimeoutExpired:
        changed, pushed = _worktree_state(wt, branch)
        return {"ticket": issue["identifier"], "status": "blocked", "branch": branch,
                "blocked_reason": f"worker timed out after {config.WORKER_TIMEOUT_SEC}s "
                                  f"(left {changed} uncommitted changes, pushed={pushed})",
                "summary": "timeout"}
    finally:
        try:
            os.unlink(promptfile)
        except OSError:
            pass

    text, session_id = _parse_output(out) if config.USE_RESUME else (out, None)

    m = None
    for m in _RESULT_RE.finditer(text):
        pass  # take the LAST RESULT line
    if not m:
        # RESULT guarantee: the worker ended without a result. Report the worktree state instead of a
        # bare error so partial work is visible (docs/long-running-tasks.md — "RESULT guarantee").
        changed, pushed = _worktree_state(wt, branch)
        tail = (text or out)[-300:]
        return {"ticket": issue["identifier"], "status": "blocked", "branch": branch,
                "session_id": session_id,
                "blocked_reason": f"worker ended without a RESULT line "
                                  f"(left {changed} uncommitted changes, branch pushed={pushed})",
                "summary": tail}
    try:
        res = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return {"ticket": issue["identifier"], "status": "blocked", "branch": branch,
                "session_id": session_id,
                "blocked_reason": f"unparseable RESULT: {e}", "summary": m.group(1)[:300]}
    res.setdefault("branch", branch)
    res["session_id"] = session_id or res.get("session_id")
    return res


def remove_worktree(branch):
    safe = branch.replace("/", "-")
    wt = os.path.join(config.WORKTREE_BASE, safe)
    _as_run_user(f"cd {shlex.quote(config.REPO)} && git worktree remove --force {shlex.quote(wt)}", timeout=60)
