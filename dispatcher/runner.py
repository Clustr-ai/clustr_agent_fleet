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
    """Run a bash script as RUN_USER in a clean LOGIN environment.

    The script is written to a temp file and invoked as `sudo -iu <user> bash <file>` rather than
    passed inline: `-iu` (login) is required so the Claude binary can spawn child processes (MCP
    servers, ripgrep) — under `-u -H` it fails with EACCES — and so the user's profile (where the
    worker's MCP env lives) is sourced. Passing the multi-line script as a FILE (not `bash -c '...'`)
    avoids the login shell re-quoting newlines and silently dropping all but the first statement.
    """
    fd, sf = tempfile.mkstemp(prefix="agent-run-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(sf, 0o644)  # readable by RUN_USER
    try:
        return subprocess.run(
            ["sudo", "-iu", config.RUN_USER, "bash", sf],
            capture_output=True, text=True, timeout=timeout,
        )
    finally:
        try:
            os.unlink(sf)
        except OSError:
            pass


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

    # Parallel workers share ONE app clone, so serialize the quick git step (fetch + worktree add)
    # with a lock to avoid ref-lock races; the long claude run below still proceeds fully in parallel.
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
__MCP="$(mktemp)"
python3 {shlex.quote(os.path.join(config.FLEET_REPO, "bin", "render-mcp-config.py"))} {shlex.quote(config.MCP_CONFIG)} > "$__MCP"
{shlex.quote(config.CLAUDE_BIN)} -p "$(cat {shlex.quote(promptfile)})" \
  --model {shlex.quote(config.CLAUDE_MODEL)} \
  --mcp-config "$__MCP" \
  --strict-mcp-config \
  --permission-mode bypassPermissions \
  --add-dir {shlex.quote(wt)}
rm -f "$__MCP"
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
