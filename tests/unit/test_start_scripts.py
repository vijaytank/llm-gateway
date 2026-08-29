"""
tests/unit/test_start_scripts.py — Phase 3 tests for startup packaging scripts.

Covers:
  - start-gateway.ps1 existence, valid syntax elements, and URLs
  - start-gateway.bat existence and delegation
  - start-gateway.sh existence and POSIX shell structure
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_start_gateway_ps1_content():
    ps1_path = ROOT / "start-gateway.ps1"
    assert ps1_path.exists(), "start-gateway.ps1 missing in project root"
    content = ps1_path.read_text(encoding="utf-8")
    assert "docker info" in content
    assert "docker/docker-compose.yml" in content
    assert "--profile" in content and "full" in content
    # Port is now read from .env (UiPort variable) rather than hardcoded — check both
    assert "UiPort" in content or "http://localhost:4002" in content
    assert "http://localhost:4000" in content
    assert "Start-Process" in content


def test_start_gateway_bat_content():
    bat_path = ROOT / "start-gateway.bat"
    assert bat_path.exists(), "start-gateway.bat missing in project root"
    content = bat_path.read_text(encoding="utf-8")
    assert "start-gateway.ps1" in content
    assert "ExecutionPolicy Bypass" in content


def test_start_gateway_sh_content():
    sh_path = ROOT / "start-gateway.sh"
    assert sh_path.exists(), "start-gateway.sh missing in project root"
    content = sh_path.read_text(encoding="utf-8")
    assert "#!/usr/bin/env bash" in content
    assert "docker info" in content
    assert "docker/docker-compose.yml" in content
    assert "--profile" in content and "full" in content
    assert "http://localhost:4002" in content
    assert "xdg-open" in content or "open" in content
