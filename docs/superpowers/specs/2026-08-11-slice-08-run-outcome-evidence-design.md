---
slice_id: "slice-08-run-outcome-evidence"
title: "A run's outcome is judged on evidence, not on the exit code of a process that was only watching"
status: SPEC_APPROVED
target_version: "2.19.0"
depends_on: []
lenses:
- wondelai/release-it#health-checks-and-observability@34ac73394a51
- wondelai/release-it#stability-patterns@34ac73394a51
- wondelai/design-everyday-things#two-gulfs@82db4899450a
---

# Slice 08 — The outcome is what the world shows, not what the client returned

## 1. Problem

`runner.py`'s module docstring states the design and its reason:

> Deriving status from an exit code is the point. Previously the agent was
> asked, in its prompt, to set its own terminal status — so an agent that
> crashed or simply forgot left the slice stranded with no way back.

That reasoning was right and it is not being revisited. What has changed is the
identity of the process whose exit code is read. Under the shipped harness the
argv is `opencode run …`, and `opencode run` is a **thin client to a long-lived
server**. Its exit says the client stopped. The session — the thing doing the
work — is not a child of this runner and does not end when its client does.

So `_record_outcome` answers a question nobody asked. It reads *"did that
process end, and how"* and files the answer under *"did the work finish, and
how"*.

### 1.1 The shallow check is standing in for the deep one, which already exists

Reading `health-checks-and-observability`, health checks come in two flavours:
**shallow** (process alive) and **deep** (dependencies reachable, resources
available). The exit code is a shallow check. "Did commits land on the branch
`close-slice` merges" is a deep one.

This repository already has the deep check. `_unmet_postcondition` is exactly
that, it is well built, and slice 07 paid for it. But look at where it is wired
(`runner.py:338`):

```python
missing = ""
if exit_code == 0:
    missing = _unmet_postcondition(...)
```

**The deep check runs only when the shallow one already said OK.** That is
inverted. A deep check earns its cost precisely in the cases the shallow one
gets wrong, and here it is switched off in every one of them. When the client
exits non-zero the pipeline never looks at the world at all: it goes straight to
the gate status, the `on_<role>_failed` hook, and the sandbox teardown.

### 1.2 What it has cost, three times, measured

| Date | Shape | Cost |
| --- | --- | --- |
| 2026-08-10 | planner exited 1 on a billing limit **after writing a 60 KB plan** (#30) | a finished document that no supported command could certify |
| 2026-08-10 | executor killed by a machine reboot mid-run | status walked by hand in the consuming repo |
| 2026-08-11 | client died on `uv_spawn` at 72 s; the session worked on for 4½ minutes (#29) | slice returned to its gate over a **complete, green, seven-task run**; the slice's containers swept out from under the live agent; status walked by hand again |

The third case is the one to design against, because a re-dispatch was the
natural next action and the lock had already been released. Two agents in one
worktree was one command away, and what prevented it was a human saying so.

### 1.3 The operator cannot see any of this — which is the same defect

`two-gulfs` names the second half. The **gulf of evaluation** is the gap between
what the system did and what the operator understands happened; it is bridged by
visible feedback and honest system-state indicators.

Everything this pipeline shows across that gulf currently misreports the
2026-08-11 run:

* the log line said `executor exited 1; status -> PLAN_APPROVED` while the agent
  was mid-task-1-of-7;
* `status` showed the slice back at its gate, i.e. *nothing is running*;
* `wait` returned **0**, the same code a fully successful run returns (#26).

That last one is not a cosmetic complaint about exit codes. `wait --dir . ...`
exists to be backgrounded so a harness can act on the result without polling. A
signal that cannot distinguish "the plan is ready" from "the run died" leaves the
caller with no bridge at all. This is why #26 belongs in this slice rather than
in a tidy-up of its own: it is the same wrong equation, seen from the operator's
side instead of the runner's.

## 2. Non-goals

1. **Re-opening who may certify what.** 2.17.0 reserved `EXECUTION_COMPLETE` to
   the supervisor because an executor once reported "7/7 green" over five red
   tests. Nothing here returns a claim *about the world* to the party doing the
   work.
2. **Making the runner understand opencode.** The session's own log would answer
   the liveness question directly, and reading it would bind the supervisor to
   one harness. Every signal used below is harness-agnostic.
3. **Retrying or resuming a dead run.** Repair stays a human act.
4. **Changing what `close-slice` merges or how.**

## 3. Design

### 3.1 The outcome becomes three-valued

Today the verdict is a two-valued function of one input. It becomes a
three-valued function of three:

```
verdict = f(exit code, deep check, liveness)
```

| exit | deep check | liveness | verdict | today |
| --- | --- | --- | --- | --- |
| 0 | met | — | **success** | success |
| 0 | unmet | — | **returned to gate** | same |
| ≠0 | **met** | — | **success** | *failure* ← wrong |
| ≠0 | unmet | quiet | **returned to gate** | same, for the right reason |
| ≠0 | unmet | **active** | **unknown** | *failure* ← wrong, and destructive |

Rows three and five are the change. Row three is nearly free — the deep check is
already written — and on its own it retires the 2026-08-11 case: seven commits
were on the branch when the verdict was taken.

**`unknown` is a first-class outcome, not an error.** It writes no status, fires
no completion hook, tears down nothing, and says plainly that the run's fate was
not observed. The asymmetry justifying it: an over-cautious `unknown` costs a
human one reading; a wrong `failure` costs a destroyed stack, a false record and
an invited re-dispatch into a live session.

### 3.2 Liveness, without knowing what the agent is

The only harness-agnostic fact about a working agent is that **it changes its
workspace**. Both roles have one the runner already knows:

* isolated → the worktree at `.worktrees/<slice_id>`;
* producing → the directory named by `produces`.

So after a non-zero exit with the deep check unmet, the runner watches that
workspace's newest modification time. Quiet for the settle window → the agent is
gone, verdict `returned to gate`. Still moving → `unknown`.

This is not a new invention: it is what a human did by hand on 2026-08-11
(a watcher that returned after 420 s with no new activity) before daring to
touch the slice. Formalising a step an operator already has to perform is the
better argument for it than novelty would be.

### 3.3 Two windows, and why the guessing is bounded

`stability-patterns` frames timeouts as the mechanism that **reclaims stuck
resources**, and the teardown is exactly a reclamation. The lens's warning is
about firing too late; ours fired while the resource was in use. Both are cured
by bounding the wait rather than by removing it:

* **settle window** — how long the workspace must be quiet before the agent is
  presumed gone. Default 300 s. Wrong-short is the dangerous direction, so it is
  generous.
* **observation deadline** — how long the runner will keep watching an active
  workspace before returning `unknown` anyway. Default 1800 s. Its purpose is to
  stop the runner from becoming the thing that never ends.

Both live under `state_machine` in `agents.yaml`, both defaulted, neither
required. **The runner keeps the slice lock while it watches**, which is a second
reason to prefer waiting: the lock is what makes a re-dispatch refuse, and the
2026-08-11 near-miss was possible only because the lock had already been
released.

### 3.4 A document a dead run finished (#30)

Row three of the table settles this for isolated roles, because a commit is an
atomic act of completion: if it is on the branch, writing it finished.

It does **not** settle it for producing roles, and the reason is recorded in this
repo already — a produced document is born `PLAN_DRAFTING` precisely because the
agent writes the file the moment it starts typing (#21). *A file exists* is
therefore evidence that there is something to read, not that anyone finished
writing it. Promoting on file-existence alone would certify half-written plans,
which is the failure `produced_status` was introduced to prevent.

The verdict for that case is `unknown`, and `unknown` needs an exit. `reconcile`
is not it: it moves a document **back** to its gate and says so in its docstring.
The produced document is left at `PLAN_DRAFTING`, whose only edge is
`→ PLAN_GENERATED`, writable only by the supervisor that died.

So this slice adds one human-owned transition — call it `certify`:

```
certify --file <produced document>
```

It asserts *"I read this and it is complete."* This does not weaken §2.1's
guarantee, and the difference is what the claim is about. `EXECUTION_COMPLETE`
asserts something about the world, is checkable by a third party, and is
sometimes overstated by the party asked — reserving it is a guard against a lie.
`PLAN_GENERATED` asserts only that writing stopped. No observer can determine
that once the writer is gone; the exit code was never a weaker measurement of it
but a measurement of something else that usually correlates. A human who has read
the document is a better instrument than a process signal, and the real quality
gate — `approve-plan` — is untouched and still comes after.

### 3.5 `wait` says which thing happened (#26)

`_report_wait_result` returns `0` for `OUTCOME_TERMINAL` whatever the terminal
status is. It gains the ability to distinguish, using
`abandonment.success_statuses(config)` — verified present at
`scripts/abandonment.py:45`, in a module `_report_wait_result` already imports
for its `OUTCOME_*` constants, so nothing new has to be reached for:

| code | meaning | change |
| --- | --- | --- |
| 0 | reached a **success** status | narrowed |
| 1 | timed out | unchanged |
| 2 | abandoned | unchanged |
| 3 | watcher could not reach a verdict | unchanged |
| **4** | the dispatch ended **without** reaching a success status | new |
| **5** | the run's fate was not observed (`unknown`) | new |

`4` is the code the 2026-08-09 planner run needed, and `5` is the one the
2026-08-11 executor run needed. `--until-success` must return on both rather
than waiting out a clock: after 2.17.0 a failed dispatch lands on its **gate**,
where no process will ever move it again, so waiting for success is waiting
forever.

## 4. Compatibility

Every default preserves today's behaviour except where today's behaviour is the
defect. The new exit codes are additive; `0` becomes *narrower*, which can only
convert a silent misread into a visible one. A caller that already treats
non-zero as "look at it" is unaffected.

## 5. What must be tested

1. **Non-zero exit, commits on the branch → success.** The 2026-08-11 case,
   reduced: a fake agent that commits and then exits 1.
2. **Non-zero exit, no commits, workspace quiet → returned to gate.** Today's
   behaviour must survive, and for the stated reason rather than by accident.
3. **Non-zero exit, no commits, workspace still changing → `unknown`**, and:
   status unchanged, no completion hook, **`sandbox.tear_down` not called**. The
   teardown assertion is the one that matters; it is the destructive half.
4. **Observation deadline reached with the workspace still active → `unknown`**,
   not `failure`, and the runner returns.
5. **The lock is held for the whole observation** and released on every path.
6. **`certify` on a document at `PLAN_DRAFTING`** moves it to `PLAN_GENERATED`;
   on a document at any other status it refuses and names the status.
7. **`wait` exit codes** — one test per row of §3.5, including that a run
   returned to its gate no longer exits 0.
8. **A producing role that exits non-zero with only a half-written document**
   yields `unknown`, never `PLAN_GENERATED`.

Tests 1 and 3 are the slice; if only two could be written, those are the two.

## 6. Risks

1. **The settle window is a bet.** A pathological agent that thinks for longer
   than 300 s without touching a file is declared gone. Accepted: the verdict is
   then `returned to gate`, which is today's verdict for that case anyway, so the
   window can only improve on the status quo.
2. **A watching runner is a running process.** Bounded by the observation
   deadline, and it holds the lock while it waits, which is a feature.
3. **`unknown` slices accumulate if nobody looks.** They are visible in `status`
   by construction — that is the point — but a slice at an in-progress status
   with no live supervisor is what `reconcile` already exists to sweep.
4. **Workspace mtime is a coarse instrument on Windows.** Directory mtimes do not
   always propagate; the implementation must walk, not stat the root.
