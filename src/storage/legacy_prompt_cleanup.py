"""Retire legacy prompt tables once, before the server starts its workers."""
import gzip
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import time


def clear_legacy_prompt_tables():
    if os.getenv("POSTGRESQL_URI") or os.getenv("MONGODB_URI"):
        return None
    path = Path(os.getenv("CREDENTIALS_DIR", "./creds")) / "credentials.db"
    if not path.is_file():
        return None

    def quoted(name):
        return '"' + name.replace('"', '""') + '"'

    with closing(sqlite3.connect(str(path), timeout=30)) as db:
        schemas = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        retired = [name for name in schemas if name == "marked_prompts" or name.startswith("marked_prompts_backup_")]
        if not retired:
            return None
        expected = {"id", "text", "model", "created_at", "chars", "preview"}
        for name in retired:
            columns = {row[1] for row in db.execute("PRAGMA table_info(" + quoted(name) + ")")}
            if columns != expected:
                raise RuntimeError("Unexpected legacy prompt schema: " + name)

        def preserved_data():
            tables = {}
            for name, sql in schemas.items():
                if name in retired:
                    continue
                columns = [row[1] for row in db.execute("PRAGMA table_info(" + quoted(name) + ")")]
                rows = list(db.execute("SELECT * FROM " + quoted(name) + " ORDER BY " + ",".join(map(quoted, columns))))
                tables[name] = {"schema": sql, "columns": columns, "rows": rows}
            sequence_exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
            sequences = list(db.execute("SELECT * FROM sqlite_sequence ORDER BY name")) if sequence_exists else []
            return {"tables": tables, "sequences": sequences}

        db.execute("BEGIN IMMEDIATE")
        before = preserved_data()
        backup_dir = path.parent / "backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup_path = backup_dir / ("accounts-before-prompt-cleanup-" + str(time.time_ns()) + ".json.gz")
        content = json.dumps(before).encode("utf-8")
        descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(gzip.compress(content))
        if gzip.decompress(backup_path.read_bytes()) != content:
            raise RuntimeError("Account backup verification failed")

        size_before = path.stat().st_size
        db.execute("PRAGMA secure_delete=OFF")
        for name in retired:
            db.execute("DROP TABLE " + quoted(name))
        if preserved_data() != before:
            raise RuntimeError("Unrelated data changed during prompt cleanup")
        db.commit()
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if preserved_data() != before:
            raise RuntimeError("Account verification failed after compaction")
        return {"removed_tables": len(retired), "account_backup": str(backup_path),
                "bytes_before": size_before, "bytes_after": path.stat().st_size}
