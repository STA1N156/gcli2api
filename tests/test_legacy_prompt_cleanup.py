from contextlib import closing
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.storage.legacy_prompt_cleanup import clear_legacy_prompt_tables


class LegacyPromptCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "credentials.db"
        self.environment = patch.dict("os.environ", {"CREDENTIALS_DIR": str(self.root), "POSTGRESQL_URI": "", "MONGODB_URI": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE credentials(id INTEGER PRIMARY KEY AUTOINCREMENT, credential_data TEXT)")
            db.execute("INSERT INTO credentials VALUES(42,'legacy')")
            db.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='credentials'")
            db.execute("CREATE TABLE antigravity_credentials(id INTEGER PRIMARY KEY, disabled INTEGER, model_cooldowns TEXT, error_codes TEXT)")
            db.execute("INSERT INTO antigravity_credentials VALUES(7,1,'{\"m\":9999999999}','[429]')")
            db.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO config VALUES('ttl','3600')")
            db.execute("CREATE TABLE session_bindings(binding_key TEXT PRIMARY KEY,filename TEXT,expires_at REAL)")
            db.execute("INSERT INTO session_bindings VALUES('session','account.json',9999999999)")
            for name in ("marked_prompts", "marked_prompts_backup_20261111_191910"):
                db.execute(f"CREATE TABLE {name}(id TEXT PRIMARY KEY,text TEXT,model TEXT,created_at REAL,chars INTEGER,preview TEXT)")
                db.execute(f"INSERT INTO {name} VALUES('id',?,'m',1,1048576,'sample')", ("x" * 1048576,))
            db.commit()

    def test_clears_history_compacts_and_preserves_all_other_data(self):
        result = clear_legacy_prompt_tables()
        self.assertEqual(result["removed_tables"], 2)
        self.assertLess(result["bytes_after"], result["bytes_before"])
        backup = json.loads(gzip.decompress(Path(result["account_backup"]).read_bytes()))
        self.assertEqual(set(backup["tables"]), {"credentials", "antigravity_credentials", "config", "session_bindings"})
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'marked_prompts%'").fetchall(), [])
            self.assertEqual(db.execute("SELECT * FROM antigravity_credentials").fetchone(), (7, 1, '{"m":9999999999}', '[429]'))
            self.assertEqual(db.execute("SELECT seq FROM sqlite_sequence WHERE name='credentials'").fetchone()[0], 99)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertIsNone(clear_legacy_prompt_tables())
        self.assertEqual(len(list((self.root / "backups").iterdir())), 1)

    def test_unexpected_schema_is_preserved(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("ALTER TABLE marked_prompts ADD COLUMN unrelated TEXT")
            db.commit()
        with self.assertRaisesRegex(RuntimeError, "Unexpected legacy prompt schema"):
            clear_legacy_prompt_tables()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM marked_prompts").fetchone()[0], 1)

    def test_backup_failure_prevents_deletion(self):
        with patch("src.storage.legacy_prompt_cleanup.gzip.decompress", return_value=b"invalid"):
            with self.assertRaisesRegex(RuntimeError, "backup verification failed"):
                clear_legacy_prompt_tables()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM marked_prompts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
