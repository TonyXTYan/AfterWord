# Roadmaps

Plans for where AfterWord goes after v1, and how far along each step is.

## How the files relate

| File | What it is | Who changes it |
|---|---|---|
| [outlook.md](outlook.md) | The north star: end goal, principles, phases P0 to P6, metrics, open questions. | The owner only. The other files follow it. |
| [current-state.md](current-state.md) | What AfterWord does today, what P0 still lacks, and the tech debt that will slow later phases. | Refresh when a phase closes or a large PR merges. |
| [p0-voice-out.md](p0-voice-out.md) | The short checklist that closes P0. | Tick items as they land. |
| [p1-context-bus.md](p1-context-bus.md) | The detailed plan for the next phase: schema, hooks, storage, suggestions, timeline, metric, milestones. | Tick milestones as they land; edit the design when it changes. |
| [p2-p6-later.md](p2-p6-later.md) | One section per later phase: what already exists, gaps, first spike, exit criteria. | Expand a section into its own file when that phase starts. |
| [decisions.md](decisions.md) | The open questions from outlook §8 plus new ones from the audits, each with a recommendation. | The owner moves a decision to "decided". |

If a plan file disagrees with `outlook.md`, the outlook wins. Fix the plan, or raise the change with the owner.

## Status board

Last updated: 2026-11-25.

| Phase | Status | Next milestone | Plan |
|---|---|---|---|
| P0 Voice out | Closing out. 1 of 3 "done when" clauses met (no overlap), 1 mostly (silent-reply explanations), 1 not (distinct voices by default). | P1 M1 (migration runner with the reason columns), then P0-4, then P0-1; see the order in p0-voice-out.md | [p0-voice-out.md](p0-voice-out.md) |
| P1 Context bus | Not started. Nothing recorded for replay yet. | M1: schema versioning and the `bus` table | [p1-context-bus.md](p1-context-bus.md) |
| P2 Screen context | Not started. | Spike: AX focus helper at a stable signed path | [p2-p6-later.md#p2-screen-context](p2-p6-later.md#p2-screen-context) |
| P3 Speech in | Not started. | Spike: push-to-talk with SpeechAnalyzer | [p2-p6-later.md#p3-speech-in](p2-p6-later.md#p3-speech-in) |
| P4 Suggester | Not started. | Spike: offline MLX model benchmark on recorded events | [p2-p6-later.md#p4-the-real-suggester](p2-p6-later.md#p4-the-real-suggester) |
| P5 Closed loop | Not started. | Spike: `afterword replay` over a time range | [p2-p6-later.md#p5-closed-loop-and-learning](p2-p6-later.md#p5-closed-loop-and-learning) |
| P6 Stretch | Not started. | Spike: speak a suggestion through the alert path | [p2-p6-later.md#p6-stretch-goals](p2-p6-later.md#p6-stretch-goals) |

Open decisions: 17 (D1-D17), none decided. See [decisions.md](decisions.md).

## How to update these docs

1. When a milestone lands, tick its box in the phase file and add the PR number next to it, for example `- [x] M1 ... (#7)`.
2. Update that phase's row on the status board above: the status, the next milestone, and the "Last updated" date.
3. When every milestone in a phase is ticked and its "done when" is checked, set the board status to "Done" and refresh `current-state.md`. Ask the owner to update the phase label in `outlook.md`.
4. When a decision is made, set its status in `decisions.md` to "decided", record the date and the choice, and update any plan that depended on it.
5. Keep "done when" lines checkable: a test that passes, a number in the GUI, or a thing you can demo. If a line can't be checked, rewrite it.

Code references use paths relative to the repo root, for example `afterword/daemon/log.py`. Line numbers are as of 2026-11-25 and will drift.
