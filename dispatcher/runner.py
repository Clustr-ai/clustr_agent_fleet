"""Per-issue worker runner.

The worker runs as the low-privilege RUN_USER — NOT as the dispatcher's user. The dispatcher (holds
the Linear/Resend secrets) shells out via `sudo -u <RUN_USER>`, which:
  1. creates an isolated worktree off latest main in RUN_USER's OWN app clone, and
  2. runs the headless Claude worker in it (bypass-permissions — safety is the credential boundary:
     no SSH key, App-token git that can't push main, assume-only AWS, SELECT-only prod — DESIGN.md §3).

So even a misbehaving worker cannot reach main/prod or read the dispatcher's secrets. The RESULT line
is parsed from the worker's stdout.
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
    """Run a bash script as RUN_USER via sudo. Returns CompletedProcess."""
    return subprocess.run(
        ["sudo", "-iu", config.RUN_USER, "bash", "-c", script],
        capture_output=True, text=True, timeout=timeout,
    )


def _fallback_branch(issue):
    slug = re.sub(r"[^a-z0-9]+", "-", issue.get("title", "").lower()).strip("-")[:40]
    return f"agent/{issue['identifier'].lower()}-{slug}"


def build_prompt(issue):
    tmpl = open(config.WORKER_PROMPT).read()
    return (tmpl
            .replace("{{TICKET}}", issue["identifier"])
            .replace("{{TITLE}}", issue.get("title", ""))
            .replace("{{BRANCH}}", issue.get("branchName") or _fallback_branch(issue)))


def run_worker(issue, branch):
    """As RUN_USER: make a worktree off origin/main, run the worker in it, return the parsed RESULT.

    The whole thing runs in one sudo invocation so the worktree is owned by RUN_USER throughout.
    """
    prompt = build_prompt(issue)
    safe = branch.replace("/", "-")
    wt = os.path.join(config.WORKTREE_BASE, safe)

    # The prompt (methodology + issue title — not secret) goes via a temp file readable by RUN_USER,
    # so we avoid quoting a large multi-line string through the shell.
    fd, promptfile = tempfile.mkstemp(prefix="agent-prompt-", suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write(prompt)
    os.chmod(promptfile, 0o644)

    script = f"""set -e
cd {shlex.quote(config.REPO)}
git fetch -q origin main
mkdir -p {shlex.quote(config.WORKTREE_BASE)}
if git rev-parse --verify {shlex.quote(branch)} >/dev/null 2>&1; then
  git worktree add {shlex.quote(wt)} {shlex.quote(branch)} 2>/dev/null || true
else
  git worktree add -b {shlex.quote(branch)} {shlex.quote(wt)} origin/main 2>/dev/null || true
fi
cd {shlex.quote(wt)}
{shlex.quote(config.CLAUDE_BIN)} -p "$(cat {shlex.quote(promptfile)})" \
  --model {shlex.quote(config.CLAUDE_MODEL)} \
  --mcp-config {shlex.quote(config.MCP_CONFIG)} \
  --strict-mcp-config \
  --permission-mode bypassPermissions \
  --add-dir {shlex.quote(wt)}
"""
    try:
        p = _as_run_user(script, timeout=config.WORKER_TIMEOUT_SEC)
        out = (p.stdout or "") + ("\n" + p.stderr if p.returncode != 0 and p.stderr else "")
    except subprocess.TimeoutExpired:
        return {"ticket": issue["identifier"], "status": "blocked",
                "blocked_reason": f"worker timed out after {config.WORKER_TIMEOUT_SEC}s",
                "summary": "timeout", "branch": branch}
    finally:
        try:
            os.unlink(promptfile)
        except OSError:
            pass

    m = None
    for m in _RESULT_RE.finditer(out):
        pass  # take the LAST RESULT line
    if not m:
        return {"ticket": issue["identifier"], "status": "blocked",
                "blocked_reason": "worker produced no RESULT line",
                "summary": (out[-400:] if out else "no output"), "branch": branch}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return {"ticket": issue["identifier"], "status": "blocked",
                "blocked_reason": f"unparseable RESULT: {e}", "summary": m.group(1)[:400], "branch": branch}


def remove_worktree(branch):
    safe = branch.replace("/", "-")
    wt = os.path.join(config.WORKTREE_BASE, safe)
    _as_run_user(f"cd {shlex.quote(config.REPO)} && git worktree remove --force {shlex.quote(wt)}", timeout=60)
