---
slice_id: "slice-07-isolated-dispatch-contract"
title: "Isolated dispatch: hand the agent a path both trees agree on, and check that its work landed on the branch"
status: VERIFIED_CLOSED
target_version: "2.11.0"
depends_on: []
lenses:
- wondelai/pragmatic-programmer#design-by-contract@6800c2f4dceb
- ecc/error-handling#core-error-principles@bca77baf7611
---

# Slice 07 — The Isolated Dispatch Contract

## 1. Problem

A dispatch is a contract. Read `cmd_dispatch_agent` as one and its preconditions
are in good order — four gates run before the first irreversible mutation, and
the function's own docstring says so deliberately. Its **postcondition is never
stated and never checked**, and one of its preconditions is unstated too. Both
gaps were paid for in the same run.

### 1.1 The unstated precondition: a path that names one specific tree

`orchestrator.py:419` resolves the target document to an absolute path and
`orchestrator.py:523` renders that absolute path into the agent's prompt. The
precondition nobody wrote down is *the path handed to the agent must name the
copy in the tree the agent was told to work in*. An absolute path cannot do
that: it names one tree, always the project root's, no matter which tree the
agent is standing in.

Note this is not affected by how the operator typed `--file`. The `.resolve()`
at line 419 means a relative argument and an absolute one produce the identical
prompt. There is no invocation of the current code that avoids this.

**A retraction belongs here, because an earlier version of this section had a
different and wrong explanation.** Issue #15 attributed a planner failure to
opencode refusing an absolute path as `external_directory`. That does not
survive the logs: `planner_2026-08-05-slice-06-...log` (the sixteen-minute run
that produced a complete plan), `executor_2026-08-05-slice-06-...log` and
`planner_2026-08-05-slice-07-...log` all carry an absolute path in their first
line and contain **zero** `permission requested` lines between them. Something
did refuse that one run — the log quoted in the issue is real — but it was not
the shape of the path, and the claim that it was cannot be sustained.

The evidence that would settle it no longer exists: `runner.py:69` opens the log
with mode `"w"`, so the retry overwrote the failed run's log with its own. That
is worth its own issue and is not this slice's business, but it is the reason
this section states a mechanism rather than a diagnosis.

What follows does not depend on the retracted claim.

### 1.2 The consequence that is proven: isolation silently defeated

For an **isolated** role the absolute path does not merely name an inconvenient
copy — it names the copy in the *wrong tree*, and the agent works there. The
dispatcher did its half correctly: it created
`.worktrees/slice-06-abandoned-dispatch` on `feat/slice-06-abandoned-dispatch`
and passed it as `--cwd`. The prompt then told the agent to execute the plan at
an absolute path inside the **main** tree. The agent noticed where it had landed
and said so:

```
$ git status && git branch --show-current
On branch main
Your branch is ahead of 'origin/main' by 6 commits.
main
```

It then did the work and committed it there — seven commits on `main`, a branch
a human had checked out, past no gate at all. Afterwards:

```
$ git log --oneline main..feat/slice-06-abandoned-dispatch
(empty)
```

The agent did not disobey. Its prompt carried the standing instruction not to
create a branch and to commit onto the one it was handed; it was handed a
worktree as cwd and an absolute path into a different tree as its task, and the
task won. An instruction competing with a concrete path loses to the path.

Everything isolation exists to prevent happened: work landed outside
`close-slice`, `close-slice` then had nothing to merge, two concurrent dispatches
would have collided in one tree, and the per-slice sandbox was attached to a
slice whose code was not isolated at all.

### 1.3 The unchecked postcondition: nothing counts commits

The postcondition of an isolated dispatch is *the work is on `feat/<slice_id>`,
where the next gate will look for it*. Nothing asserts it. `runner.py:120`
states the principle correctly and then removes it for the one role whose output
is code:

```python
    Exit code 0 means the process ended, not that the work landed. ...

    Skipped for an isolated role: its output lives in a worktree that has not
    been merged, so the main tree is the wrong place to look and a miss here
    would mean nothing.
```

Nothing takes its place:

```
$ grep -rn "rev-list\|log --oneline\|commits" scripts/runner.py scripts/git_ops.py
scripts/git_ops.py:76:    A repository with no commits yet answers False, which is the truthful answer
```

So for an isolated role, exit 0 goes straight to `success_status`: an executor
that produced **no commits at all** is recorded `EXECUTION_COMPLETE`, and the
next human sees a slice that says it is ready to close. This is decidable from
the code and needs no incident to prove it — though the incident in §1.2 is
exactly the shape it fails on, and the reasoning behind the skip assumes the
thing that can go wrong: "the main tree is the wrong place to look" holds only
if the agent worked in the worktree.

Note what the skip does in the meantime — it prints a note saying the check was
skipped and returns "". Logging that you are not checking is not handling; it is
a swallow wearing a diagnostic's clothes.

### 1.4 One sentence

The pipeline tells the agent where to work with a path that is only valid in one
tree, and then never checks which tree it worked in.

## 2. Scope

In:

- The document path rendered into the prompt is expressed **relative to the
  project root**, in posix form — the one form that is correct in both trees.
- Dispatch refuses, before any mutation, when the document does not lie under
  the project root.
- After a successful **isolated** dispatch, the supervisor asserts the
  postcondition: this run left commits on the slice branch. Zero → `FAILED`.
- The isolated branch of the artifact check stops skipping and starts answering
  the same question against the artifact that role actually produces.
- `status` annotates a document claiming `EXECUTION_COMPLETE` whose slice branch
  is empty.

Out:

- Judging the *content* of commits. Counting is not review; the human audit gate
  before `close-slice` is unchanged and still does the judging.
- Changing what a non-isolated role's artifact check does. It is correct.
- Diagnosing what refused the run quoted in #15. Its log was overwritten by the
  retry (§1.1), so there is nothing left to diagnose from.
- Detecting an agent that commits to the slice branch **and** the main tree. It
  passes this check and still dirties main; see §6.
- The two residues found while reading the issues: a produced document's own
  in-progress status (`PLAN_DRAFTING`, #14) and the poorer generated frontmatter
  (#16) belong to one another and to a different surface —
  `produced.frontmatter_block` — not to this slice.

## 3. Design

### 3.1 The path both trees agree on

The obvious reading of "render the path relative to the agent's cwd" forces the
worktree to exist before the prompt is composed — moving an irreversible side
effect ahead of the fallible steps, against the ordering `cmd_dispatch_agent`
maintains on purpose. It is also unnecessary.

A worktree created by `git worktree add` is a checkout of the same repository
with the same layout. Therefore `target_file.relative_to(project_root)` is
simultaneously correct in the project root (non-isolated role, cwd =
`project_root`) and in the worktree (isolated role, cwd =
`.worktrees/<slice_id>`, which contains the same tree). **One expression serves
both roles, and `project_root` is already known at line 425 — well before the
prompt is composed at 521.** No reordering.

Rendered with `as_posix()`. The tree layout is identical on both platforms, git
speaks posix, and a Windows path with backslashes travelling through a prompt
string into a CLI argument is one more thing that can be eaten in transit.

The absolute path stays exactly where it is useful — logging, the frontmatter
write, the runner's `--file`. Only the prompt changes.

### 3.2 Stating the precondition, and being honest about its reach

If the document does not lie under the project root, `relative_to` raises and
there is no correct prompt to render. Per the lens, that is the caller's half of
the contract, and the guard belongs at the boundary: refuse at dispatch time,
naming both paths, before the lock and before any status write — rather than
dispatching an agent that will be denied its own input and burn a model's quota
discovering it.

**Honest qualification, because a spec that oversells a guard invites trust it
has not earned.** `find_project_root` walks *up* from the target file, so with
today's call graph the root is always an ancestor and this branch is
unreachable. Its value is not a bug it fixes; it is that the invariant becomes
stated and stays stated the day someone passes a `project_root` in from
elsewhere. It is one branch and a message. Write it as a refusal, not an
`assert`: an operator on a CLI deserves a sentence, not a traceback.

### 3.3 The postcondition: commits *this run* added

Asserted in `runner.py`, in the exit-0 branch of `_record_outcome`, beside the
check it generalises. Not at `close-slice` — that is the later gate the lens
tells us not to defer to; and not in `cmd_dispatch_agent`, which returned long
before the agent finished.

The naive query is `git rev-list --count <main>..feat/<slice_id>`, and it is
wrong in a way the tests must pin. `create_git_worktree` returns early when the
worktree already exists and will attach to a branch that already carries commits
from an earlier dispatch. Under the naive query a re-dispatch that produced
nothing at all counts the previous run's work and passes.

So the dispatcher captures the slice branch's tip **immediately after the
worktree exists** — `HEAD` for a fresh branch, the branch's own tip for a reused
one — and passes it to the supervisor as `--base-ref`. The epilogue asks:

```
git rev-list --count <base-ref>..feat/<slice_id>
```

which is, precisely, *what this invocation added*. That is the postcondition a
dispatch can guarantee; "the branch has something on it" is a statement about
accumulated history and guarantees nothing about the run being recorded.

Zero commits → `FAILED`, with a log line naming the branch, the base ref and the
count — the operator is being told their agent's successful-looking run produced
nothing, and a verdict without its evidence is not actionable. Non-isolated
roles pass an empty `--base-ref` and are untouched.

### 3.4 One question, two artifacts

`_missing_artifact` becomes a single function answering one question — *did this
run leave what the next gate needs?* — with the artifact chosen by role:

| Role | Artifact | Check |
|---|---|---|
| declares `produces`, not isolated | a document carrying `slice_id` and a `status` | as today |
| isolated | commits on `feat/<slice_id>` since `--base-ref` | §3.3 |
| neither | nothing to check | `""` |

Returns `""` when the postcondition holds and a human-readable reason when it
does not — the contract it already has. The name should follow the widened
question; the planner may pick it.

### 3.5 `status` reports the contradiction

Same shape as the abandonment annotation shipped in 2.10.0: derived on read,
never stored, and the report mutates nothing.

```
[EXECUTION_COMPLETE ] 2026-08-05-slice-06-abandoned-dispatch-plan.md - ...
                       ⚠ feat/slice-06-abandoned-dispatch has no commits; close-slice would merge nothing
```

Worth having even with §3.3, because §3.3 only runs when a supervisor lived to
run it. A dispatch whose supervisor died leaves the document wherever it was,
and the human repair — `set-status EXECUTION_COMPLETE` — gets no commit check at
all. That repair is documented, legal, and precisely how a slice arrives at
`EXECUTION_COMPLETE` having never been verified. The report is the second net
and costs one `rev-list`.

### 3.6 Why not fix this in the prompt

The tempting alternative is to tell the executor to check it is in the worktree.
Rejected twice over. The agent already carried that instruction and followed
what it understood; adding a second instruction to compete with a concrete path
loses to the path again. And an agent's report that it complied is a
self-report — the thing this pipeline refuses everywhere else, including in the
very function being extended here.

## 4. Tests

Deterministic, against the plugin's own suite. No real dispatch, no network, no
model.

1. **Rendering.** Given an absolute `--file` under the project root, the composed
   prompt contains no drive letter, no leading separator and no occurrence of the
   project-root string, and does contain the posix relative path. Run for both an
   isolated and a non-isolated role: the rendered path is *identical* for both,
   and that identity is the property worth pinning — it is the reason §3.1 needs
   no reordering.
2. **Refusal.** A document outside the project root is refused before any
   mutation: exit non-zero, status unchanged, no lock left behind.
3. **Postcondition — zero commits.** Fixture repo with a slice branch at
   `--base-ref`: exit 0 + no new commits → status `FAILED`, and the log names
   branch, base and count.
4. **Postcondition — work landed.** One commit since `--base-ref` → success
   status, no reason string, existing behaviour otherwise unchanged.
5. **Postcondition — re-dispatch onto a branch that already has work.** Branch
   carries earlier commits, this run adds none → still `FAILED`. This is the test
   that pins base-ref semantics; the naive `main..feat/` implementation passes
   every other test here and fails this one.
6. **Non-isolated unchanged.** The document check keeps its current behaviour,
   including the `FAILED` it already produces for an invisible document.
7. **`status` annotation.** `EXECUTION_COMPLETE` + empty branch → annotated;
   `EXECUTION_COMPLETE` + commits → output byte-for-byte as today; the command
   mutates neither document nor lock.
8. **Integration, stub adapter.** An isolated dispatch invokes the agent with a
   cwd inside `.worktrees/` **and** a prompt path relative to it. This is the
   test that would have caught the incident in §1.2, and the reason to write it
   at the integration level rather than only unit-testing the formatter.

## 5. Documentation and version

`docs/configuration.md` gains a short statement of the dispatch contract: what a
dispatch promises for each role, what it now verifies, and what it still does
not (content of the work — that is the human audit before `close-slice`). Plus
the operational tell for anyone debugging a run on an older version: if the
first `git branch --show-current` in an executor log says anything other than
`feat/<slice_id>`, isolation did not take.

Version **2.11.0**. No new command and no changed invocation, but a real
behaviour change: a dispatch that previously recorded `EXECUTION_COMPLETE` with
an empty branch now records `FAILED`. That is a bug fix and belongs in a minor
bump and the changelog, not a patch release that nobody reads.

## 6. Risk, stated plainly

**A legitimate zero-commit run now reads as `FAILED`.** A plan that is entirely
verification with no code change is possible. The trade is deliberate and
asymmetric: `FAILED` is recoverable — the machine already allows
`FAILED -> PLAN_APPROVED` and the run is re-dispatched — while a false
`EXECUTION_COMPLETE` is not detectable at all by anyone downstream. Per the
lens, this is a thing that *might* happen, so it gets error handling rather than
an assertion: the message states exactly what was counted and between which
refs, so the human can judge it in one read.

**Counting is not reviewing.** Five junk commits pass this check. It raises the
floor from "the process exited" to "the process left something where the next
gate looks", and claims nothing beyond that.

**The relative path assumes the worktree mirrors the project tree.** True for
`git worktree add`, which is the only way this plugin creates one. A future
adapter that runs an agent somewhere else invalidates the assumption silently,
so it belongs as a comment where the path is computed — not only in this
document.

**An agent that commits to both trees still passes.** This slice removes the
known cause and detects the known outcome; it does not detect work that landed
correctly *and* additionally dirtied main. Catching that needs a before/after
snapshot of the main tree's branch, which is more machinery than the observed
failure justifies. If it is ever observed, it is its own slice and this one
supplies the measurement.
