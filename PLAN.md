# Audit Trail Store (DuckDB) - Phase Plan

Driver checklist for `/dev-orchestrate`. Full spec:
[plans/2026-06-30-audit-trail-store.md](plans/2026-06-30-audit-trail-store.md).

Each phase N below maps to **Task N** in that spec doc. Before implementing a
phase, read that task's section (its Files list, numbered steps, and its own
verification step) - it is the source of truth for what the phase must deliver.
The per-task bash verification steps are part of the definition of done; the
`## Verify` command below is the automated regression gate.

## Phases
- [x] 1. Dependency, module skeleton, DB bootstrap
- [x] 2. Transcript parser (pure, unit-tested)
- [x] 3. Incremental ingester (`ingest [--watch]`)
- [x] 4. Search, stats, tokens CLI (Phase-1 shippable milestone)
- [x] 5. File versions + content-addressed blobs
- [x] 6. Reconstruction CLI (`cat` / `diff`)
- [ ] 7. Docs + dependency note + version bump

Deferred, out of scope this run: Task 8 (TUI search view).

## Verify
~/.pyenv/versions/global/bin/python -m pytest -q
