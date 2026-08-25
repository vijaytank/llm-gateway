## Summary

<!-- What does this PR change and why? One or two sentences. -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change (fix or feature that changes existing behavior/config)
- [ ] Documentation only

## Checklist

- [ ] `pytest tests/unit/` passes locally (CI enforces the 70% coverage gate)
- [ ] `ruff check gateway brain adapter ui schemas wizard scripts tests` clean
- [ ] `CHANGELOG.md` updated under "Unreleased" (if user-facing)
- [ ] If `schemas/config.py` changed: migration / `gateway_config.yaml` updated
- [ ] No secrets, absolute personal paths, or new unpinned dependencies
