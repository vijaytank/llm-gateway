"""
wizard/setup.py — Full CLI setup wizard (Phase 4 deliverable 1).

The 7 wizard steps:
  1. API keys for cloud providers (nvidia / groq / cerebras / openrouter)
  2. Which providers are enabled
  3. Deployment mode (docker | bare-metal)
  4. Local models (enable, ollama/vllm base URLs)
  5. Custom providers (name, base URL, auth type, models via discovery,
     free/premium flag) — each validated with a live probe before saving
  6. UI admin password (stored bcrypt-hashed in Postgres ui_settings)
  7. Service install (systemd unit on Linux / LaunchAgent on macOS /
     Docker instructions on Windows)

Outputs:
  - .env                (chmod 600, enforced on every startup check)
  - gateway_config.yaml (validated against GatewayConfig before writing)
  - service unit file   (via wizard/install_linux.py or install_macos.py)

Idempotent: re-running prompts for confirmation before overwriting.

Usage:
    python wizard/setup.py [--scripted --api-keys '{...}' --force]
"""

import os
import sys
import json
import secrets
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from schemas.config import (
    GatewayConfig,
    ProviderConfig,
    CustomProviderConfig,
    create_default_config,
)
from wizard.provider_probe import list_models, probe_provider


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def prompt_with_default(prompt_text: str, default: str) -> str:
    user_input = input(f"{prompt_text} [default: {default}]: ").strip()
    return user_input if user_input else default


def get_yes_no(prompt_text: str, default: bool = True) -> bool:
    default_str = "Y/n" if default else "y/N"
    while True:
        user_input = input(f"{prompt_text} ({default_str}): ").strip().lower()
        if not user_input:
            return default
        if user_input in ("y", "yes"):
            return True
        if user_input in ("n", "no"):
            return False
        print("  Please answer y/yes or n/no.")


def write_file_with_permissions(path: Path, content: str, mode: int = 0o600) -> None:
    # Normalize to Unix LF so Docker containers don't get \r in env values on Windows.
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(content, encoding="utf-8", newline="\n")
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows filesystems ignore POSIX modes — permission enforcement is
        # a documented Linux/macOS guarantee (plan Issue 12).
        print(f"  Note: POSIX permissions not applicable on this OS for {path.name}")
    print(f"  Written {path} with mode {oct(mode)}")


def collect_api_keys() -> dict:
    """Wizard question 1: API keys."""
    print("\n=== Step 1/7: API Keys ===")
    keys = {}
    for name, env_var, blurb in [
        ("NVIDIA", "NVIDIA_API_KEY", "free tier NIM endpoints"),
        ("Groq", "GROQ_API_KEY", "free tier Groq endpoints"),
        ("Cerebras", "CEREBRAS_API_KEY", "free tier Cerebras endpoints"),
        ("OpenRouter", "OPENROUTER_API_KEY", "free tier OpenRouter (charge-risk aware)"),
    ]:
        value = input(f"  {name} API key ({blurb}) [skip]: ").strip()
        if value:
            keys[env_var] = value
    return keys


def choose_providers(config: GatewayConfig) -> None:
    """Wizard question 2: provider enablement."""
    print("\n=== Step 2/7: Cloud Providers ===")
    for name in ("nvidia", "groq", "cerebras", "openrouter"):
        pc: ProviderConfig = getattr(config.providers, name)
        pc.enabled = get_yes_no(f"  Enable {name}?", default=pc.enabled)


def choose_deployment_mode() -> str:
    """Wizard question 3: docker vs bare-metal (drives .env URLs & install)."""
    print("\n=== Step 3/7: Deployment Mode ===")
    while True:
        mode = prompt_with_default("  Deployment mode (docker / bare-metal)", "docker").lower()
        if mode in ("docker", "bare-metal", "baremetal", "bare"):
            return "docker" if mode == "docker" else "bare-metal"
        print("  Please answer 'docker' or 'bare-metal'.")


def choose_local_models(config: GatewayConfig) -> None:
    """Wizard question 4: local models (Phase 3 integration)."""
    print("\n=== Step 4/7: Local Models ===")
    config.providers.local.enabled = get_yes_no("  Enable local models (Ollama/vLLM)?", default=False)
    if config.providers.local.enabled:
        config.providers.ollama_base_url = prompt_with_default(
            "  Ollama base URL", config.providers.ollama_base_url)
        vllm = prompt_with_default("  vLLM base URL (blank = disabled)", "")
        config.providers.vllm_base_url = vllm


def choose_custom_providers(config: GatewayConfig) -> None:
    """Wizard question 5: custom OpenAI-compatible providers, probed live."""
    print("\n=== Step 5/7: Custom Providers ===")
    while get_yes_no("  Add a custom OpenAI-compatible provider?", default=False):
        name = input("  Provider name (lowercase, digits, - or _): ").strip().lower()
        base_url = input("  Base URL (e.g. https://host/v1): ").strip()
        auth_type = ""
        while auth_type not in ("none", "bearer", "header"):
            auth_type = prompt_with_default("  Auth type (none/bearer/header)", "bearer")
        api_key_env = ""
        if auth_type != "none":
            api_key_env = input("  Env var holding the API key (e.g. MYPROVIDER_API_KEY): ").strip()
        tier = ""
        while tier not in ("free", "premium"):
            tier = prompt_with_default("  Tier (free/premium)", "free")

        print("  Probing provider...")
        discovery = list_models(base_url=base_url, auth_type=auth_type,
                                api_key_env=api_key_env, timeout=12.0)
        if discovery.ok and discovery.models:
            print(f"  Discovered {len(discovery.models)} model(s): "
                  + ", ".join(discovery.models[:10]))
            models = discovery.models
        else:
            manual = input("  Discovery failed/empty. Enter comma-separated model names: ").strip()
            models = [m.strip() for m in manual.split(",") if m.strip()]
            if not models:
                print("  ✗ No models given — skipping this provider.")
                continue
            chat = probe_provider(base_url=base_url, model=models[0],
                                  auth_type=auth_type, api_key_env=api_key_env)
            if not chat.ok:
                print(f"  ✗ Chat probe failed ({chat.error}) — skipping this provider.")
                continue
        try:
            cp = CustomProviderConfig(
                name=name, base_url=base_url, api_key_env=api_key_env,
                auth_type=auth_type, tier=tier, models=models,
            )
        except Exception as e:
            print(f"  ✗ Invalid provider ({e}) — skipping.")
            continue
        if any(p.name == cp.name for p in config.custom_providers):
            print(f"  ✗ Provider '{cp.name}' already added — skipping duplicate.")
            continue
        config.custom_providers.append(cp)
        print(f"  ✓ Added {cp.name} with {len(cp.models)} model(s).")


def _read_password(prompt_text: str) -> str:
    """Read a password without echoing characters to stdout, with cross-platform safety."""
    if not sys.stdin.isatty():
        try:
            line = sys.stdin.readline()
            return line.strip()
        except Exception:
            return ""
    import getpass
    try:
        return getpass.getpass(prompt_text)
    except (EOFError, KeyboardInterrupt):
        return ""


def choose_ui_password(is_docker: bool = False) -> None:
    """Wizard question 6: UI admin password (hashed into Postgres later)."""
    print("\n=== Step 6/7: Web UI Admin Password ===")
    if is_docker:
        print("  Tip: In Docker mode, you can also set the admin password on first visit to http://localhost:4002.")
    while True:
        try:
            pw = _read_password("  Choose UI admin password (min 8 chars, blank to set on first UI visit): ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Skipped — you'll set the password on first visit to the UI.")
            return
        if not pw:
            print("  Skipped — you'll set the password on first visit to the UI.")
            return
        if len(pw) < 8:
            print("  Too short (min 8). Try again.")
            continue
        try:
            confirm = _read_password("  Confirm password: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Skipped — you'll set the password on first visit to the UI.")
            return
        if pw != confirm:
            print("  Passwords don't match. Try again.")
            continue
        _set_ui_password(pw)
        return


def _set_ui_password(password: str) -> None:
    """Hash and store the admin password in ui_settings (DB must be reachable)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from ui.auth import set_password

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("GATEWAY_DB_URL", "")
    if not db_url:
        print("  DATABASE_URL not set — password will be set on first UI visit instead.")
        return
    engine = create_engine(db_url)
    # Ensure table exists even before alembic runs (idempotent create_all).
    from schemas.db import Base
    Base.metadata.create_all(engine, tables=[__import__("schemas.db", fromlist=["UiSetting"]).UiSetting.__table__])
    with Session(engine) as session:
        set_password(session, password)
    print("  ✓ Admin password stored (bcrypt-hashed).")


def choose_service_install() -> None:
    """Wizard question 7: background service installation."""
    print("\n=== Step 7/7: Background Service ===")
    if not get_yes_no("  Install as a background service now?", default=True):
        print("  Skipped. You can run it later with: python -m wizard.install_linux|install_macos|install_windows")
        return
    import platform
    system = platform.system()
    if system == "Linux":
        from wizard.install_linux import install
        install(project_root=ROOT)
    elif system == "Darwin":
        from wizard.install_macos import install
        install(project_root=ROOT)
    else:
        from wizard.install_windows import show_instructions
        show_instructions()


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------

def generate_env_file(env_path: Path, keys: dict, mode: str = "docker") -> None:
    """Generate .env with chmod 600. Postgres password is generated, never a
    placeholder (psycopg2 would treat it literally)."""
    pg_password = keys.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(16)
    db_host = "postgres" if mode == "docker" else "localhost"
    redis_host = "redis" if mode == "docker" else "localhost"
    db_url = f"postgresql://llm_gateway:{pg_password}@{db_host}:5432/llm_gateway"

    from cryptography.fernet import Fernet
    enc_key = keys.get("SECRET_ENCRYPTION_KEY") or Fernet.generate_key().decode("utf-8")
    session_secret = keys.get("SESSION_SECRET") or secrets.token_urlsafe(32)
    master_key = keys.get("LITELLM_MASTER_KEY") or f"sk-litellm-{secrets.token_urlsafe(32)}"

    excluded_keys = {
        "POSTGRES_PASSWORD", "SECRET_ENCRYPTION_KEY", "SESSION_SECRET",
        "LITELLM_MASTER_KEY", "DATABASE_URL", "GATEWAY_DB_URL", "REDIS_URL", "GATEWAY_REDIS_URL"
    }

    lines = [
        "# LLM Gateway Environment Variables",
        "# Generated by wizard/setup.py",
        "",
        *[f"{k}={v}" for k, v in sorted(keys.items()) if k not in excluded_keys],
        "",
        f"DATABASE_URL={db_url}",
        f"GATEWAY_DB_URL={db_url}",
        f"REDIS_URL=redis://{redis_host}:6379/0",
        f"GATEWAY_REDIS_URL=redis://{redis_host}:6379/0",
        f"POSTGRES_PASSWORD={pg_password}",
        f"SECRET_ENCRYPTION_KEY={enc_key}",
        "# UI session secret",
        f"SESSION_SECRET={session_secret}",
        "",
        "# LiteLLM master key",
        f"LITELLM_MASTER_KEY={master_key}",
        "",
    ]
    write_file_with_permissions(env_path, "\n".join(lines), mode=0o600)


def generate_config_yaml(config_path: Path, config: GatewayConfig) -> None:
    write_file_with_permissions(config_path, config.to_yaml(), mode=0o644)


def validate_config(config_path: Path) -> bool:
    """Validate generated config using the canonical GatewayConfig model."""
    print("\n=== Configuration Validation ===")
    try:
        config = GatewayConfig.load_from_file(str(config_path))
        print("  Config validated successfully!")
        print(f"  Schema version: {config.meta.schema_version}")
        enabled = [n for n in ("nvidia", "groq", "cerebras", "openrouter", "local")
                   if getattr(config.providers, n).enabled]
        print(f"  Enabled builtin providers: {enabled}")
        print(f"  Custom providers: {[p.name for p in config.custom_providers]}")
        print(f"  Virtual models: {len(config.virtual_models)}")
        return True
    except Exception as e:
        print(f"  ERROR: Config validation failed: {e}")
        return False


def enforce_env_permissions(env_path: Path) -> None:
    """Re-check .env permissions on every wizard start (plan Issue 12)."""
    if not env_path.exists():
        return
    try:
        current = os.stat(env_path).st_mode & 0o777
        if current != 0o600 and os.name == "posix":
            os.chmod(env_path, 0o600)
            print(f"  Fixed .env permissions: {oct(current)} → 0o600")
    except OSError:
        pass


def run_db_init() -> None:
    """Run Alembic migrations and seed model registry."""
    print("\n=== Database Initialization ===")
    print("  Running Alembic migrations...")

    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print(f"  ERROR: Alembic migration failed: {result.stderr}")
        sys.exit(1)
    print("  Alembic migrations completed successfully.")

    print("  Seeding model registry...")
    from scripts.seed_model_registry import seed_model_registry
    result = seed_model_registry()
    if result.get("status") == "success":
        print(f"  Model registry seeded: {result.get('models_seeded', 0)} models added")
    else:
        print(f"  Warning: Model registry seeding: {result.get('message', 'unknown error')}")


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM Gateway Setup Wizard")
    parser.add_argument("--scripted", action="store_true",
                        help="Run in scripted mode with predetermined values")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Alias for --scripted (run headlessly)")
    parser.add_argument("--docker", action="store_true",
                        help="Assume Docker deployment mode (skip mode selection)")
    parser.add_argument("--regenerate-config", action="store_true",
                        help="Regenerate .env and config headlessly from defaults/existing .env")
    parser.add_argument("--api-keys", type=str, default="",
                        help="JSON string of API keys for scripted mode")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing .env and config files without confirmation")
    args = parser.parse_args(argv)

    is_headless = args.scripted or args.non_interactive or args.regenerate_config

    project_root = Path.cwd()
    env_path = project_root / ".env"
    config_path = project_root / "gateway_config.yaml"

    print("=== LLM Gateway Setup Wizard ===\n")

    # Idempotency: never overwrite without explicit confirmation unless force or headless
    if env_path.exists() and config_path.exists() and not args.force and not is_headless:
        print(f"  .env already exists at {env_path}")
        print(f"  gateway_config.yaml already exists at {config_path}")
        if not get_yes_no("Do you want to overwrite these files?"):
            print("  Aborting setup. Existing files preserved.")
            return 0

    enforce_env_permissions(env_path)

    config = create_default_config()

    # Determine deployment mode
    if args.docker or is_headless:
        deploy_mode = "docker"
    else:
        deploy_mode = None

    # Q1: API keys
    keys = {}
    if args.api_keys:
        try:
            keys = json.loads(args.api_keys)
        except Exception:
            pass
    elif is_headless:
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    k = k.strip()
                    # Strip inline comments (e.g. KEY=value  # note) so comment
                    # text is never embedded into regenerated secret values.
                    v = v.split(" #", 1)[0].strip()
                    if k in ("NVIDIA_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "POSTGRES_PASSWORD", "SECRET_ENCRYPTION_KEY", "SESSION_SECRET", "LITELLM_MASTER_KEY"):
                        keys[k] = v
    else:
        keys = collect_api_keys()

    # Q2–Q5 (interactive only)
    if not is_headless:
        choose_providers(config)
        if deploy_mode is None:
            deploy_mode = choose_deployment_mode()
        choose_local_models(config)
        choose_custom_providers(config)
    else:
        if deploy_mode is None:
            deploy_mode = "docker"

    # Generate .env BEFORE DB-touching steps so DATABASE_URL is available
    generate_env_file(env_path, keys, mode=deploy_mode)
    for key_name, key_value in keys.items():
        os.environ[key_name] = key_value

    # Q6: UI admin password (interactive bare-metal or optional in docker)
    if not is_headless:
        choose_ui_password(is_docker=(deploy_mode == "docker"))

    # Write + validate config (validated BEFORE it is trusted)
    generate_config_yaml(config_path, config)
    if not validate_config(config_path):
        if not is_headless:
            if not get_yes_no("Config invalid. Continue anyway?", default=False):
                sys.exit(1)
        else:
            print("  ERROR: Config validation failed in headless mode.")
            sys.exit(1)

    # DB init: Skip for Docker mode (handled by docker-db-init container)
    if deploy_mode == "docker":
        print("\n=== Database Initialization ===")
        print("  Skipping host database initialization (will run inside Docker db-init container).")
    else:
        try:
            run_db_init()
        except Exception as e:
            print(f"  WARNING: DB init had issues: {e}")
            if not is_headless:
                if not get_yes_no("Continue anyway?", default=False):
                    sys.exit(1)

    # Q7: service install (interactive only)
    if not is_headless:
        choose_service_install()

    print("\n=== Setup Complete ===")
    print(f"  .env file: {env_path}")
    print(f"  Config file: {config_path}")
    print("\nNext steps:")
    if deploy_mode == "docker":
        print("  1. Review gateway_config.yaml")
        print("  2. Run: docker compose --profile full up -d  (or run start-gateway.ps1 / .sh)")
        print("  3. UI: http://localhost:4002  ·  Gateway: http://localhost:4000  ·  Adapter: http://localhost:4001")
    else:
        print("  1. Start Postgres and Redis")
        print("  2. Run: python gateway/main.py  (port 4000)")
        print("  3. Run: python adapter/server.py  (port 4001)")
        print("  4. Run: python ui/app.py  (port 4002)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
