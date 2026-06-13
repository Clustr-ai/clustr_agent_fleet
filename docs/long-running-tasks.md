# Long-running tasks: pause, checkpoint, continue

Status: **PROPOSED** (design, not yet built). Companion to [DESIGN.md](../DESIGN.md).

## Problem

A single worker run is bounded — by wall-clock timeout, by the model's context window, and by the
fact that an agent reaches *natural* stopping points (it finishes a coherent phase, or it needs
something external). Today a worker ends in exactly two ways: `success` (PR up → AI Review) or
`blocked` (gave up → AI Blocked). A task too big for one run therefore looks like a *failure*, and the
only recovery is a fresh agent that re-derives everything from scratch.

We want long tasks to make **incremental, resumable progress** across multiple runs without a human in
the loop, and without paying to rebuild the agent's understanding every time.

## Key insight: the expensive thing is the mental model, not the work

When a worker stops, the costly thing to recover is *the plan, what's done, what's left, which files
matter, and the decisions already made*. A continuation that re-explores the repo to reconstruct that
is the anti-pattern. **The whole design is about making the mental model cheap to persist and cheap to
rehydrate.** Three existing properties already help:

1. **The worktree persists** (`KEEP_WORKTREES=1`) — partial work (commits, new files) is on disk, so a
   continuing agent *sees* what was done via `git diff`/`git log` instead of imagining it.
2. **The model auto-compacts** — a single run summarizes itself near the context limit; the hard wall is
   the wall-clock timeout, not an abrupt context overflow.
3. **The tracker is durable state** — the issue + its comments already survive across runs.

So continuation is "add a disciplined handoff on top of persistent state," not "rebuild from zero."

## Design

### 1. A third outcome: `paused`

Extend the `RESULT` the worker emits. The agent is *instructed* to stop cleanly and checkpoint at a
natural boundary (finished a phase, near budget, or needs something external) — pausing becomes
first-class and expected, not a failure.

```jsonc
RESULT: {
  "ticket": "ENG-123",
  "status": "success | blocked | paused | decomposed",
  "branch": "agent/eng-123-...",

  // when paused:
  "pause_reason": "more_runtime | waiting_external | needs_human",
  "session_id": "<claude session id, for --resume>",   // optional fast-path
  "journal": ".agent/ENG-123.md",                       // path in the worktree
  "progress": "3/5 steps",                              // human-readable
  "next": "wire the import endpoint to the new service",
  "wait_for": "ci | staging_deploy | human_reply",      // waiting_external / needs_human
  "question": "Should imports dedupe on email or external id?",  // needs_human

  // when decomposed:
  "subtasks": ["ENG-201", "ENG-202"],

  // existing:
  "pr_url": "...", "staging_deployed": false, "summary": "...", "blocked_reason": "..."
}
```

### 2. The journal — a small structured handoff, not a transcript

The worker maintains `.agent/<ticket>.md` in the worktree and updates it at every checkpoint. This is
the **rehydration unit**: a few hundred tokens that replace an hour of re-exploration. A short version
is mirrored as an issue comment, so it doubles as human-readable progress.

```markdown
# ENG-123 — <title>
## Goal            <one paragraph>
## Plan (checklist)
- [x] add the migration
- [x] new repository method
- [ ] wire the endpoint        ← NEXT
- [ ] frontend hook
- [ ] tests
## Done so far     <what changed + why, file:line>
## Key files       <the 4-6 files that matter>
## Decisions / constraints
## Open questions / blockers
## Resume hint     session=<id>  OR  "rehydrate from git diff + this file"
```

`.agent/` is **gitignored** so it never pollutes the PR, but it persists on disk because the worktree
persists. (Decision #1 below: gitignored+mirrored vs committed.)

### 3. Continuation: resume-first, rehydrate-as-fallback (the layered pattern)

- **Fast path — session resume.** If a `session_id` is present and the session is still local/fresh,
  continue with `claude --resume <id>`. Zero re-derivation — the agent keeps its own context.
- **Durable fallback — journal + worktree.** If the session is gone (reboot, expiry, different worker),
  a *new* agent starts from: issue + journal + `git diff origin/main` + "continue from NEXT." Cheap,
  robust, survives anything. **The journal is always maintained**, so even the fallback never
  re-explores from scratch.

Best of both: resume when you can, rehydrate when you must.

### 4. Dispatcher continuation loop (bounded, branch by reason)

On `paused`, the dispatcher re-dispatches automatically — a big task becomes *N bounded runs chained
without a human*. Branch on `pause_reason`:

| reason | dispatcher action |
|---|---|
| `more_runtime` | re-dispatch a continuation immediately (same worktree; resume or rehydrate). Stay In Progress; refresh the lease. |
| `waiting_external` | move to **AI Awaiting Input**, re-check the condition (CI / staging deploy) on a poll; resume when satisfied. |
| `needs_human` | move to **AI Awaiting Input** + post the `question`; when a human replies (new comment), resume with that reply as input. |

A **continuation budget** caps the loop (default 6). Exceed it → `AI Blocked` with "continuation budget
exhausted — needs decomposition." Continuation count + `session_id` + branch live in a per-ticket
sidecar (`~/.agent-state/<ticket>.json`) so they survive a dispatcher restart.

### 5. State-machine additions

- **`AI Awaiting Input`** (new) — paused on an external condition or a human question. Distinct from
  `In Progress` (actively working) and `AI Blocked` (failed). Keeps the board honest and, importantly,
  **the lease sweeper ignores it** (it's a legitimate wait, not a stall).
- `more_runtime` continuations stay in `In Progress`; the dispatcher refreshes the lease each round so
  the sweeper doesn't mistake a mid-continuation gap for a crash.

### 6. Decomposition for true epics

Some tickets aren't "long," they're *epics* — continuation-of-one-agent is the wrong tool. The agent's
first run can instead **plan and fan itself out**: write the plan, create linked **sub-tickets** (via
the tracker MCP), and emit `status: "decomposed"`. The sub-tickets flow into `AI Ready` and are worked
as separate, parallel, independently-reviewable tasks — which the fleet already does well. Rule of
thumb:

- fits in a few chained runs → `paused` / auto-continue;
- genuinely an epic → decompose into sub-tickets.

## Implementation phases

1. **Chained continuation (highest value, smallest).** `paused: more_runtime` + journal + auto-continue
   loop + continuation budget + sidecar state. Rehydration via journal+diff (no resume yet). This alone
   makes large tasks chain to completion.
2. **Session-resume fast path.** Store `session_id`; continue via `claude --resume`. Pure optimization.
3. **External waits + human input.** `waiting_external` / `needs_human` + the `AI Awaiting Input` status
   + comment-driven resume.
4. **Decomposition.** `decomposed` outcome + sub-ticket creation + the "if too big, plan and fan out"
   worker-prompt guidance.

## Files touched

- `worker-prompt.md` — checkpoint discipline, journal maintenance, emit `paused`, "decompose if too big."
- `dispatcher/runner.py` — parse extended RESULT; `--resume`/rehydration prompt builder; read/write sidecar.
- `dispatcher/dispatcher.py` — continuation loop, branch-by-reason, budget, lease refresh, status moves.
- `dispatcher/config.py` — `MAX_CONTINUATIONS`, `STATUS_AI_AWAITING_INPUT`, sidecar dir.
- `dispatcher/linear_api.py` — create sub-issue, read latest comment after a marker, set Awaiting Input.
- worker `.gitignore` — ignore `.agent/`.
- `example.env`, `DESIGN.md` — new config + docs.

## Open decisions

1. **Journal location** — gitignored + mirrored to a comment (clean PRs) vs committed under `.agent/`
   (travels with the branch, visible to reviewers). *Recommend gitignored + comment mirror.*
2. **Resume reliability** — trust `claude --resume` as the primary continuation, or treat it as an
   optimization over journal-rehydration? *Recommend journal-first (robust), resume as opt-in.*
3. **Continuation budget** (default 6) and whether per-continuation timeout differs from a normal run.
4. **`AI Awaiting Input`** as a dedicated status vs a label on `In Progress`. *Recommend a status* (the
   sweeper needs to tell "waiting" from "working").
5. **Decomposition trigger** — agent decides autonomously vs a human flags "too big." *Recommend agent
   proposes sub-tickets, human still gates them into `AI Ready` (same entry gate as everything).*
