"""
tests/unit/test_wizard.py — Phase 4 wizard tests (plan: test_wizard_*.py).

Covers: config generation validity, .env permissions, idempotency,
custom provider flow, and the installer renderers (unit-level; no systemd
calls — rendering functions only).
"""

import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Config generation (test_wizard_generates_valid_config.py)
# ---------------------------------------------------------------------------

def test_generated_config_passes_schema_validation(tmp_path, monkeypatch):
    from schemas.config import create_default_config, GatewayConfig
    cfg = create_default_config()
    path = tmp_path / "gateway_config.yaml"
    from wizard.setup import generate_config_yaml
    generate_config_yaml(path, cfg)
    loaded = GatewayConfig.load_from_file(str(path))
    assert loaded.meta.schema_version == "1.0"
    assert len(loaded.virtual_models) == 3


def test_env_file_written_with_0600(tmp_path):
    from wizard.setup import generate_env_file
    env_path = tmp_path / ".env"
    generate_env_file(env_path, {"GROQ_API_KEY": "gsk_test"}, mode="docker")
    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    if os.name == "posix":
        assert mode == 0o600
    content = env_path.read_text()
    assert "GROQ_API_KEY=gsk_test" in content
    assert "LITELLM_MASTER_KEY=sk-litellm-" in content
    # Postgres password is generated, never a placeholder
    assert "***" not in content
    pw_line = [l for l in content.splitlines() if l.startswith("POSTGRES_PASSWORD=")][0]
    assert len(pw_line.split("=", 1)[1]) >= 16


def test_env_urls_follow_deployment_mode(tmp_path):
    from wizard.setup import generate_env_file
    env_docker = tmp_path / "docker.env"
    generate_env_file(env_docker, {}, mode="docker")
    assert "@postgres:5432" in env_docker.read_text()

    env_bare = tmp_path / "bare.env"
    generate_env_file(env_bare, {}, mode="bare-metal")
    assert "@localhost:5432" in env_bare.read_text()


# ---------------------------------------------------------------------------
# Idempotency (test_wizard_idempotent.py)
# ---------------------------------------------------------------------------

def test_existing_files_preserved_without_confirmation(tmp_path, monkeypatch):
    """Running setup twice: second run must NOT overwrite without 'y'."""
    from schemas.config import create_default_config
    marker = tmp_path / "gateway_config.yaml"
    original = create_default_config()
    original.routing_defaults.moving_avg_window = 77  # sentinel value
    original.save_to_file(str(marker))
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://x\n")  # both files exist

    import wizard.setup as setup_mod
    monkeypatch.chdir(tmp_path)
    # Simulate user answering "n" to the overwrite prompt
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    # Call main with argv forced to no --force
    monkeypatch.setattr(sys, "argv", ["setup.py"])
    rc = None
    try:
        setup_mod.main()
    except SystemExit as e:
        rc = e.code
    assert rc == 0 or rc is None
    # File untouched — sentinel still present
    from schemas.config import GatewayConfig
    assert GatewayConfig.load_from_file(str(marker)).routing_defaults.moving_avg_window == 77


# ---------------------------------------------------------------------------
# Custom provider flow (test_wizard_custom_provider.py)
# ---------------------------------------------------------------------------

def test_custom_provider_probe_flow_adds_to_config():
    from schemas.config import create_default_config, CustomProviderConfig, GatewayConfig
    from gateway.config_generator import get_models_from_registry

    cfg = create_default_config()
    cp = CustomProviderConfig(
        name="acme", base_url="https://api.acme.dev/v1",
        api_key_env="ACME_API_KEY", models=["vendor-model"],
    )
    cfg.custom_providers.append(cp)

    model_list = get_models_from_registry(cfg)
    entry = next(m for m in model_list if m["model_name"] == "acme-auto")
    assert entry["litellm_params"]["api_base"] == "https://api.acme.dev/v1"
    assert entry["model_info"]["provider"] == "acme"


def test_custom_provider_validation_rejects_bad_names_and_urls():
    from schemas.config import CustomProviderConfig
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        CustomProviderConfig(name="Bad Name", base_url="https://x.dev/v1", models=["m"])
    with pytest.raises(pydantic.ValidationError):
        CustomProviderConfig(name="ok", base_url="ftp://x.dev", models=["m"])
    with pytest.raises(pydantic.ValidationError):
        CustomProviderConfig(name="ok", base_url="https://x.dev/v1")  # no models


def test_disabled_custom_provider_excluded_from_model_list():
    from schemas.config import create_default_config, CustomProviderConfig
    from gateway.config_generator import get_models_from_registry
    cfg = create_default_config()
    cfg.custom_providers.append(CustomProviderConfig(
        name="offco", base_url="https://o.dev/v1", models=["m"], enabled=False))
    names = [m["model_name"] for m in get_models_from_registry(cfg)]
    assert "offco-auto" not in names


def test_api_key_read_from_env_not_config(monkeypatch):
    """API keys must come from env at generate time — never stored in YAML."""
    from schemas.config import create_default_config, CustomProviderConfig
    from gateway.config_generator import get_models_from_registry
    monkeypatch.setenv("SECRET_PROVIDER_KEY", "sk-live-abc123")
    cfg = create_default_config()
    cfg.custom_providers.append(CustomProviderConfig(
        name="secretco", base_url="https://s.dev/v1", models=["m"],
        api_key_env="SECRET_PROVIDER_KEY"))
    entry = next(m for m in get_models_from_registry(cfg) if m["model_name"] == "secretco-auto")
    assert entry["litellm_params"]["api_key"] == "sk-live-abc123"
    yaml_text = cfg.to_yaml()
    assert "sk-live-abc123" not in yaml_text


# ---------------------------------------------------------------------------
# Installer renderers (no systemctl/launchctl calls — pure rendering)
# ---------------------------------------------------------------------------

def test_systemd_unit_renders_without_hardcoded_paths():
    from wizard.install_linux import _render_unit
    unit = _render_unit(Path("Z:/opt/somewhere/llm-gateway"), "/usr/bin/python3.11")
    assert "llm-gateway" in unit
    assert "/usr/bin/python3.11" in unit
    assert "WantedBy=default.target" in unit


def test_launchagent_plist_renders_valid_plist():
    from wizard.install_macos import _render_plist
    import plistlib
    data = plistlib.loads(_render_plist(Path("/Users/me/gw"), "/usr/bin/python3"))
    assert data["Label"] == "com.llmgateway"
    assert data["KeepAlive"] is True
    assert any("gateway.main" in str(a) for a in data["ProgramArguments"])


def test_windows_installer_is_instructions_only(capsys):
    from wizard.install_windows import show_instructions
    show_instructions()
    out = capsys.readouterr().out
    assert "Docker Desktop" in out


# ---------------------------------------------------------------------------
# Probe module used by both wizard and UI
# ---------------------------------------------------------------------------

def test_probe_classifies_content_filter_as_healthy(monkeypatch):
    """Issue 4: a 400 with content_filter body means endpoint is up."""
    class FakeResp:
        status_code = 400
        text = '{"error": {"type": "content_filter"}}'

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return FakeResp()

    import wizard.provider_probe as pp
    monkeypatch.setattr(pp.httpx, "post", lambda *a, **kw: FakeResp())
    result = pp.probe_provider("https://x.dev/v1", "m")
    assert result.ok and result.status_code == 400
