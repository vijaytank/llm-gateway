"""Phase 5 security + backup unit tests.

Covers plan Phase 5 test cases:
- test_ui_login_rate_limit: 6 failed logins in a minute → 6th returns 429
  (limiter logic; the HTTP path is covered by integration tests)
- test_env_permissions_check: mode 0644 → warning, 0600 → no warning
- test_config_export / test_config_import_validation: tar.gz round-trip
"""

import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ui.rate_limit import LoginRateLimiter  # noqa: E402
from gateway.env_security import check_env_permissions  # noqa: E402


# ---------------------------------------------------------------------------
# Login rate limiter (plan: 5 attempts/minute)
# ---------------------------------------------------------------------------

@pytest.fixture
def limiter(fake_redis):
    return LoginRateLimiter(fake_redis)


def test_first_five_attempts_allowed(limiter):
    for _ in range(4):
        assert limiter.check_allowed("1.2.3.4") is True
        limiter.record_failure("1.2.3.4")
    # The 5th attempt is still allowed (limit is "5 attempts per minute").
    assert limiter.check_allowed("1.2.3.4") is True


def test_sixth_attempt_blocked(limiter):
    for _ in range(5):
        limiter.check_allowed("1.2.3.4")
        limiter.record_failure("1.2.3.4")
    assert limiter.check_allowed("1.2.3.4") is False


def test_ips_are_isolated(limiter):
    for _ in range(5):
        limiter.record_failure("1.1.1.1")
    assert limiter.check_allowed("2.2.2.2") is True


def test_successful_login_resets_attempts(limiter):
    for _ in range(5):
        limiter.record_failure("9.9.9.9")
    assert limiter.check_allowed("9.9.9.9") is False
    limiter.reset("9.9.9.9")
    assert limiter.check_allowed("9.9.9.9") is True


def test_fail_open_without_redis():
    limiter = LoginRateLimiter(None)
    assert limiter.check_allowed("any") is True
    limiter.record_failure("any")  # must not raise


# ---------------------------------------------------------------------------
# .env permission check (plan: 0644 warns, 0600 silent)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX modes not applicable on Windows")
def test_env_0644_warns(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KEY=value\n")
    os.chmod(env, 0o644)
    assert check_env_permissions(str(env)) is False


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX modes not applicable on Windows")
def test_env_0600_silent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KEY=value\n")
    os.chmod(env, 0o600)
    assert check_env_permissions(str(env)) is True


def test_env_missing_file_ok(tmp_path):
    assert check_env_permissions(str(tmp_path / "nonexistent.env")) is True


# ---------------------------------------------------------------------------
# Config export/import (plan: round-trip + malformed-archive abort)
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = """
meta:
  schema_version: "1.0"
general_settings:
  master_key: "sk-litellm-master-0123456789abcdef012345"
routing_defaults: {}
providers: {}
virtual_models: []
"""


def _make_archive(path, config_yaml=MINIMAL_CONFIG,
                  registry=None, include_meta=True):
    registry = registry if registry is not None else [
        {"provider": "mock-alpha", "model_name": "alpha-primary",
         "tier": "free", "capabilities": ["general"], "enabled": True,
         "source": "builtin"},
    ]
    with tarfile.open(path, "w:gz") as tar:
        def add(name, payload):
            data = payload.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add("gateway_config.yaml", config_yaml)
        add("model_registry.json", json.dumps(registry))
        if include_meta:
            add("meta.json", json.dumps({"format_version": 1}))


def test_import_validates_config_before_touching_anything(tmp_path):
    """Malformed archived config → SystemExit, existing files unmodified."""
    from scripts.config_backup import do_import

    target = tmp_path / "gateway_config.yaml"
    target.write_text("# original\n")
    bad_archive = tmp_path / "bad.tar.gz"

    _make_archive(bad_archive, config_yaml="""
meta: {schema_version: "1.0"}
general_settings:
  master_key: "short"   # violates >=32-char entropy rule
""")

    with pytest.raises(SystemExit, match="ABORTED"):
        do_import(str(bad_archive), assume_yes=True,
                  config_path=str(target))

    assert target.read_text() == "# original\n", \
        "existing config must NOT be modified on failed import"


def test_export_produces_readable_tarball(tmp_path):
    """Export writes a tar.gz with all three sections."""
    import scripts.config_backup as cbk

    cfg = tmp_path / "gateway_config.yaml"
    cfg.write_text(MINIMAL_CONFIG)

    out = tmp_path / "backup.tar.gz"
    result = cbk.do_export(str(out), str(cfg))
    assert Path(result["out"]).exists()

    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert "gateway_config.yaml" in names
    assert "model_registry.json" in names
    assert "meta.json" in names
