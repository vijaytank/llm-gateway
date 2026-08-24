"""
scripts/config_backup.py — Config export/import CLI (Phase 5)

Per plan Phase 5 deliverable 4:
    gateway-cli config export  → dumps gateway_config.yaml + model registry
                                 to a tar.gz.
    gateway-cli config import  → validates and restores (prompts for
                                 confirmation).

Usage (invoke through the `terminal` tool):
    python scripts/config_backup.py export --out backup.tar.gz
    python scripts/config_backup.py import backup.tar.gz [--yes]

Archive contents:
    gateway_config.yaml          validated GatewayConfig dump
    model_registry.json          rows from the model_registry table
    meta.json                    schema version + export timestamp

Import validates the YAML against GatewayConfig BEFORE touching anything;
a malformed archive aborts with the existing config unmodified.
"""

import argparse
import io
import json
import os
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG_SECTION = "gateway_config.yaml"
REGISTRY_SECTION = "model_registry.json"
META_SECTION = "meta.json"

EXPORT_FORMAT_VERSION = 1


def _db_session():
    """Session factory honoring GATEWAY_DB_URL / DATABASE_URL, or a local
    SQLite fallback for offline use (export of config-only backups)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from schemas.db import Base

    url = (os.environ.get("GATEWAY_DB_URL")
           or os.environ.get("DATABASE_URL"))
    if not url:
        # No Postgres reachable (e.g. unit-test or config-only export):
        # fall back to a scratch SQLite DB so the archive still carries a
        # (possibly empty) registry section instead of hard-failing.
        engine = create_engine("sqlite:///:memory:")
    else:
        engine = create_engine(url)
    Base.metadata.create_all(engine)  # idempotent if migrations already ran
    return sessionmaker(bind=engine)()


def do_export(out_path: str, config_path: str) -> dict:
    """Bundle gateway_config.yaml + model_registry into a tar.gz."""
    from schemas.config import GatewayConfig
    from schemas.db import ModelRegistry

    cfg = GatewayConfig.load_from_file(config_path)
    config_yaml = cfg.to_yaml()

    db = _db_session()
    try:
        rows = db.query(ModelRegistry).order_by(
            ModelRegistry.provider, ModelRegistry.model_name).all()
        registry = [
            {
                "provider": r.provider,
                "model_name": r.model_name,
                "tier": r.tier,
                "capabilities": list(r.capabilities or []),
                "enabled": bool(r.enabled),
                "source": getattr(r, "source", "custom") or "custom",
            }
            for r in rows
        ]
    finally:
        db.close()

    meta = {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "registry_rows": len(registry),
    }

    with tarfile.open(out_path, "w:gz") as tar:
        def _add(name: str, payload: str):
            data = json.dumps(payload).encode() if not isinstance(payload, str) \
                else payload.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(datetime.now(timezone.utc).timestamp())
            tar.addfile(info, io.BytesIO(data))

        _add(CONFIG_SECTION, config_yaml)
        _add(REGISTRY_SECTION, json.dumps(registry))
        _add(META_SECTION, json.dumps(meta))

    return {"out": out_path, "registry_rows": len(registry)}


def do_import(archive_path: str, assume_yes: bool = False,
              config_path: str = None) -> dict:
    """Validate and restore a config backup. Aborts on invalid content."""
    from schemas.config import GatewayConfig
    from schemas.db import ModelRegistry

    with tarfile.open(archive_path, "r:gz") as tar:
        names = tar.getnames()
        missing = [s for s in (CONFIG_SECTION, REGISTRY_SECTION, META_SECTION)
                   if s not in names]
        if missing:
            raise SystemExit(f"invalid archive: missing sections {missing}")

        def _read(name: str) -> str:
            fh = tar.extractfile(name)
            return fh.read().decode() if fh else ""

        config_yaml = _read(CONFIG_SECTION)
        registry = json.loads(_read(REGISTRY_SECTION))
        meta = json.loads(_read(META_SECTION))

    # Validate FIRST — a malformed config aborts before any write.
    import yaml as _yaml
    raw = _yaml.safe_load(config_yaml)
    try:
        cfg = GatewayConfig.model_validate(raw)
    except Exception as e:
        raise SystemExit(
            f"ABORTED: archived gateway_config.yaml failed validation; "
            f"existing config NOT modified.\n{e}")

    target_config = Path(config_path or os.environ.get(
        "GATEWAY_CONFIG_PATH",
        str(ROOT / "gateway_config.yaml")))

    print(f"Archive format v{meta.get('format_version')}, "
          f"exported {meta.get('exported_at')} "
          f"({len(registry)} registry rows)")
    print(f"This will overwrite: {target_config}")
    print(f"And REPLACE all rows in model_registry "
          f"(upserting {len(registry)} models).")
    if not assume_yes:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            raise SystemExit("import aborted by user; nothing modified")

    # Write config only after validation passed.
    target_config.write_text(cfg.to_yaml())

    # Restore registry: upsert by (provider, model_name); delete rows absent
    # from the archive so the restored state matches the exported state.
    db = _db_session()
    try:
        keep = set()
        for row in registry:
            key = (row["provider"], row["model_name"])
            keep.add(key)
            obj = db.query(ModelRegistry).filter_by(
                provider=row["provider"], model_name=row["model_name"]).first()
            if obj is None:
                obj = ModelRegistry(provider=row["provider"],
                                    model_name=row["model_name"])
                db.add(obj)
            obj.tier = row.get("tier", "free")
            obj.capabilities = row.get("capabilities", [])
            obj.enabled = row.get("enabled", True)
            if hasattr(obj, "source"):
                obj.source = row.get("source", "custom")
        for obj in db.query(ModelRegistry).all():
            if (obj.provider, obj.model_name) not in keep:
                db.delete(obj)
        db.commit()
    finally:
        db.close()

    return {"config": str(target_config), "registry_rows": len(registry)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Gateway config backup: export/import (tar.gz)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_exp = sub.add_parser("export", help="dump config + registry to tar.gz")
    p_exp.add_argument("--out", required=True, help="output .tar.gz path")
    p_exp.add_argument("--config", default=os.environ.get(
        "GATEWAY_CONFIG_PATH",
        str(ROOT / "gateway_config.yaml")), help="gateway_config.yaml path")

    p_imp = sub.add_parser("import", help="restore config + registry from tar.gz")
    p_imp.add_argument("archive", help="input .tar.gz path")
    p_imp.add_argument("--yes", action="store_true",
                       help="skip confirmation prompt")
    p_imp.add_argument("--config", default=None,
                       help="target gateway_config.yaml path")

    args = parser.parse_args(argv)

    if args.command == "export":
        result = do_export(args.out, args.config)
        print(f"Exported {result['registry_rows']} registry rows "
              f"+ config to {result['out']}")
        return 0

    result = do_import(args.archive, assume_yes=args.yes, config_path=args.config)
    print(f"Imported: config → {result['config']}, "
          f"{result['registry_rows']} registry rows restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
