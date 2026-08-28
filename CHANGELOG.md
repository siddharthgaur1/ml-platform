# Changelog

## [Unreleased]

### Fixed
- `872e8f6` — all three deployment configs (`Dockerfile`, `docker-compose.yml`,
  `deploy/fraud/`, `deploy/features/`) were broken by the repo merge; paths
  still pointed at the pre-merge layout of the three absorbed repos.

### Added
- `ffb33ce` — CI status badge, license badge, and a portfolio nav link in the
  README.

## [0.1.0] — repo merge

`featurestore`, `ml-monitor`, and `stream-fraud-detector` merged into a single
package (`src/ml_platform/`) so the three subsystems could depend on each
other directly instead of through duck-typed adapters and optional imports.
Closed three bugs that existed only because the repos were separate:

- `featurestore`'s drift detection was a documented stub (the real
  implementation lived in a "not-yet-built sibling project").
- `ml-monitor`'s feature-store adapter matched `offline_store.read()` and
  `registry.get_feature_group()` structurally instead of importing the real
  types.
- `stream-fraud-detector` wrapped `import ml_monitor` in a bare
  `try/except ImportError` that silently returned `None`, so a missing
  dependency disabled monitoring without any error.

Added `src/ml_platform/pipeline.py`, the first code path that crosses all
three subsystem boundaries, and `tests/test_pipeline_end_to_end.py`, which
could not have existed before the merge. See the README's "What integrates,
concretely" and "Limitations" sections for what the merge did and did not fix
(notably: it removed ~0 duplicate lines — the repos shared a naming skeleton,
not implementations).

Full pre-merge history lives in `docs/absorbed/` (original READMEs, RUNBOOK,
SECURITY of each of the three repos).
