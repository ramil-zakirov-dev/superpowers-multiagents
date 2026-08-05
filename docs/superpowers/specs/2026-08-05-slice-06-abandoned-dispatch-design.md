---
slice_id: "slice-06-abandoned-dispatch"
title: "Abandoned dispatch: notice a supervisor that never came back, say so, and offer a way out"
status: VERIFIED_CLOSED
target_version: "2.10.0"
depends_on: []
lenses:
- wondelai/release-it#stability-patterns@34ac73394a51
---

# Slice 06 — Abandoned Dispatch

## 1. Problem

A dispatched role's supervisor (`scripts/runner.py`) is the only thing that
writes a document's terminal status. If it dies before its epilogue, nothing
notices — ever. The document keeps claiming work is in progress, and every
reader believes it.

This is measured, not hypothetical. In one slice of a consumer project, driven
end to end through this plugin on 2026-08-04/05, **both** dispatches ended this
way:

```
$ grep -n "\[runner\]\|status ->\|exit code" .superpowers/logs/*.log
(none in either log)
```

Neither `planner_*.log` nor `executor_*.log` contains a single `[runner]` line
— not the success summary, not the ERROR branch for a refused transition, not
the `no success_status configured` WARNING. So this is not
`update_frontmatter_status` declining a move and it is not a config gap: the
runner never reached `_record_outcome` at all. The executor's log stops at
23:44:30 while that agent's own commits are timestamped 00:03:43 and 00:06:59
— it worked for another twenty-two minutes with nobody watching.

The two documents ended in different kinds of wrong:

- **The plan** stayed at `EXECUTING`. That is at least *loud*, eventually:
  `close-slice` refuses, because `EXECUTING -> VERIFIED_CLOSED` is not a legal
  transition. A human is standing at that gate and finds out.
- **The spec** stayed at `PLANNING`, and nothing was ever going to notice.
  `close-slice` targets the plan and deliberately does not touch the spec, so
  no gate downstream reads it again. A slice that was designed, planned,
  implemented, verified and closed displayed its design document as *planning
  in progress* — and it took a human reading the file and asking why.

The escape today is a human typing `set-status` by hand. For the plan that
means writing `EXECUTION_COMPLETE`, whose defined meaning is "the agent process
exited 0" — which is false, since it was killed. One slice required two such
writes. The workaround is easy, which is exactly why it will be used, and every
use puts an untrue fact into a document whose whole purpose is to be trusted.

Why the supervisor died is **not** part of this slice and is not claimed to be
known. The plugin cannot prevent an operator from stopping a run, a machine
from sleeping, or a harness from reaping a process tree. What it can stop doing
is *presenting an unknown state as a known one*. Applying
`release-it#stability-patterns` — "Let It Crash: a clean restart often beats
limping along in an unknown state" — the fix is not to make the crash
impossible, it is to make it detectable, honest and recoverable.

## 2. Scope

In:

- `status` cross-checks a document sitting at some role's `in_progress_status`
  against whether that slice's supervisor is still alive, and marks the
  contradiction instead of reporting the stored value as fact.
- A named recovery operation that moves such a document out of the in-progress
  state without asserting an outcome that did not happen.
- `wait`, and `dispatch --wait`, so a caller can be notified when a dispatch
  ends — including when it ends by abandonment.

Out:

- Diagnosing or preventing supervisor death. See §6.
- Any change to what `dispatch` does by default. It stays non-blocking; a
  blocking dispatch would hold the caller's turn for the whole run and, since
  the supervisor is deliberately detached, would leave the agent running with
  nobody to record it — the failure this slice exists to handle.
- Resuming or re-attaching to a running agent. Recovery here means recording
  what is known, not adopting an orphan.

## 3. Design

### 3.1 Abandonment is a derived fact, never a stored one

A slice is **abandoned** when both hold:

1. its document's `status` equals the `in_progress_status` of some configured
   role, and
2. no live supervisor owns it — the slice's lock is absent, or its `pid` is not
   alive.

Both halves already exist in the codebase and neither is new machinery. The
role's `in_progress_status` comes from the merged config (`PLANNING` for the
planner, `EXECUTING` for the executor, and whatever a project has redefined —
so the check must never hardcode either literal). Liveness is
`scripts/utils._is_process_alive`, today imported only by `scripts/locks.py:70`
where it already governs lock reclamation. The lock self-heals; the status does
not, and the status is what the gates read.

Abandonment is computed on read. It is never written into a document — a stored
"abandoned" flag would itself go stale the moment someone re-dispatches.

### 3.2 `status` reports the contradiction

Today `status` prints the stored value with no cross-check. It gains one
annotation, and only for the abandoned case:

```
[EXECUTING          ] 2026-08-04-selector-decision-coherence.md - ...
                       ⚠ abandoned: supervisor pid 41676 is gone; run `reconcile`
```

The stored status is still shown — it is a fact about the document, and hiding
it would be its own lie. What changes is that the report no longer implies the
work is running.

`status` stays read-only and must not repair anything it finds. A report that
silently mutates state is a worse instrument than one that lies quietly.

### 3.3 `reconcile` — the way out, without a false claim

```
usage: orchestrator.py reconcile --file <document> [--dir DIR] [--yes]
```

Legal only when §3.1 holds. It moves the document to `FAILED`, which is the one
existing status that describes *the dispatch* truthfully: the supervisor did
not report an outcome. From `FAILED` the machine already allows
`SPEC_APPROVED` and `PLAN_APPROVED`, so the human re-enters at the gate they
choose.

`FAILED` deliberately says nothing about the *work*. In the observed slice the
work was in fact complete and good; in another it will be half-written. The
plugin cannot tell those apart and must not guess — that is the human's audit,
and it is the audit the pipeline already requires before `close-slice`.

Two properties matter more than the mechanism:

- It refuses when a live supervisor owns the slice. Reconciling a running
  dispatch would race the runner's own epilogue.
- It prints what it based the decision on — the lock's pid, its liveness, the
  status it moved from — because the operator is being asked to trust a verdict
  about a process they cannot see.

Reconcile also releases the stale lock. Steady state: the artifact of a dead
run should not outlive the run.

### 3.4 `wait` — a join, so abandonment is found in bounded time

```
usage: orchestrator.py wait --slice <slice_id> [--dir DIR] [--timeout S] [--poll S]
```

Blocks while the slice is in progress and a live supervisor owns it. Exits:

| Outcome | Exit |
|---|---|
| status left the role's `in_progress_status` | `0` |
| §3.1 holds — abandoned | `2` |
| `--timeout` elapsed with neither | `1` |

Its last line names the terminal status, the elapsed time and the log path, so
the caller needs no second command. On the abandoned branch it names
`reconcile` rather than leaving the operator to look it up.

`dispatch <role> <file> --wait` is dispatch followed by wait in one process.
This is the form a harness actually backgrounds: a caller that backgrounds
plain `dispatch` is notified the instant the supervisor is *spawned*, which is
precisely the useless signal it has today. Default behaviour is unchanged.

Default `--poll` 15s: the watched thing takes minutes, so a tighter loop only
burns wakeups. Default `--timeout` is none — the caller backgrounding it has
its own, and per the lens a timeout belongs at the boundary that can act on it.

### 3.5 Why not "just make the runner more robust"

Worth stating because it is the obvious objection. `runner.py` is already
hardened for this environment: the agent writes straight into the log handle
rather than through a relay that could crash; `_safe_print` swallows the
`ValueError` a closed stdout raises in a detached job; a log-open failure is
converted into a synthesized non-zero exit precisely so `_record_outcome` still
runs. The failure observed here is not a missing `try`. It is the process
ceasing to exist between two statements — which no amount of in-process care
can cover. That is why the answer is external detection, not another guard.

## 4. Tests

Against the plugin's own suite (26 files under `tests/`), all deterministic —
no real dispatch, no sleeping on wall-clock.

- **Abandonment predicate.** Alive pid + in-progress status → not abandoned.
  Dead pid + in-progress status → abandoned. Dead pid + terminal status → not
  abandoned. Missing lock + in-progress status → abandoned. A project whose
  `agents.yaml` renames `in_progress_status` is detected by the renamed value
  and not by the literal `EXECUTING` — this is the regression that keeps the
  check honest for anyone who is not us.
- **`status`.** Annotates only the abandoned case; leaves every other row byte
  for byte as today; mutates nothing (assert the document and the lock are
  unchanged after the call).
- **`reconcile`.** Moves in-progress → `FAILED`; refuses when the supervisor is
  alive; refuses on a terminal status; removes the lock; is idempotent in the
  sense that a second run refuses cleanly rather than corrupting.
- **`wait`.** Exit 0 when the status changes underneath it; exit 2 when the pid
  dies with the status unchanged; exit 1 on timeout. Liveness and the clock are
  injected, so these are fast and not flaky.
- **`dispatch --wait`.** The flag is threaded and the default stays
  non-blocking — asserted on the existing dispatch integration test rather than
  by starting a real agent.

## 5. Documentation and version

`docs/configuration.md` gains a short section: dispatch returns immediately by
design; to be notified, background `dispatch --wait`; if a run is abandoned,
`status` says so and `reconcile` is the way out. It should state outright that
a hand-rolled waiter is a trap on Windows — `kill -0` in Git Bash reports a
live native pid as "No such process" (measured on Windows 11, pid 1336 alive
per `Get-Process`), so such a loop reports completion on its first iteration,
every time, failing open.

Version `2.10.0`: additive commands and one new annotation, no breaking change
to any existing invocation.

## 6. Risk, stated plainly

**This slice does not fix the cause.** It converts a silent wrong state into a
loud one with a documented exit. If supervisors keep dying, we will now see it
every time instead of once by accident — which is the point, and also the
instrumentation that makes finding the cause possible. If the abandoned rate
turns out to be high rather than incidental, that is the signal to open a slice
about supervisor survival, and this one will have supplied the measurement.

**`FAILED` will read harshly for a run whose work was fine.** That is a
deliberate trade: the alternative is a status that claims an outcome nobody
observed, which is the defect being removed. The plan's own audit gate is where
the work gets judged, and it already exists.

**Detection is only as good as the lock.** A dispatch whose lock was deleted by
hand looks abandoned even while running. The predicate treats a missing lock as
abandonment on purpose — a slice nothing owns is exactly the state a human
should be told about — but it means lock files are now load-bearing for a
second reason, and `reconcile`'s refusal-when-alive cannot protect a run whose
lock is gone.
