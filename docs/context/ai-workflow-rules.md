# AI Workflow Rules

## Approach

Build the redesign incrementally against these context files, one milestone at a time (M0–M6 in `progress-tracker.md`). The context files define what to build (`project-overview.md`), how (`architecture.md`, `code-standards.md`) and where things stand (`progress-tracker.md`). Implement against them. Don't infer or invent behaviour, especially counting rules, output fields or confidence rules.

## Scoping Rules

- Work on one feature unit at a time, normally one pipeline stage or one API capability.
- Prefer small, verifiable increments over large speculative changes.
- Don't combine unrelated system boundaries in a single implementation step.

## When to Split Work

Split an implementation step if it combines:

- A prompt or model change with a change to deterministic checks. Each must be measurable on its own.
- Pipeline-stage changes with API, queue or review-page changes.
- Behaviour not clearly defined in the context files, such as a new counting rule or output field.

If a change cannot be verified end to end quickly, the scope is too broad: split it.

## Handling Missing Requirements

- Don't invent product behaviour not defined in the context files.
- If a requirement is ambiguous, resolve it in the relevant context file before implementing.
- If a requirement is missing, add it as an open question in `progress-tracker.md` before continuing.

## Protected Files

Don't modify the following unless explicitly instructed:

- `../TRAINING_SAMPLES/`: the original bills.
- `../TRAINING_SAMPLES_extracted/`: the answer key. Change it only through the approved M0 migration or review-page corrections.
- `.env` and anything holding secrets.

## Keeping Docs in Sync

Update the relevant context file whenever implementation changes:

- System architecture, boundaries or pipeline stages
- Storage model decisions
- Code conventions or standards
- Feature scope, the output contract or counting rules

## Before Moving to the Next Unit

1. The current unit works end to end within its defined scope.
2. No invariant defined in `architecture.md` was violated.
3. `progress-tracker.md` reflects the completed work.
4. `pytest -q -m "not integration"` passes.
5. From M0 onwards: the answer-key scorer shows no regression in trusted precision or line recall.
