"""Final coverage push: env_security + install_windows + metrics server."""

import os
import sys

import pytest

from gateway.env_security import check_env_permissions


def test_env_security_group_readable_warns_posix(tmp_path):
    """0644 → warning (False); 0600 → silent (True). Windows: skip."""
    if sys.platform == "win32":
        pytest.skip("POSIX modes not applicable on Windows")
    env = tmp_path / ".env"
    env.write_text("K=v\n")
    os.chmod(env, 0o644)
    assert check_env_permissions(str(env)) is False
    os.chmod(env, 0o600)
    assert check_env_permissions(str(env)) is True


def test_install_windows_instructions(capsys):
    from wizard.install_windows import show_instructions
    show_instructions()
    out = capsys.readouterr().out
    assert "Docker Desktop" in out


def test_metrics_server_factory():
    """start_metrics_server wires the app without needing a live port."""
    from brain.metrics import GatewayMetrics, start_metrics_server, get_metrics_port
    m = GatewayMetrics()
    runner = start_metrics_server(m, get_metrics_port())
    assert runner is not None
