"""Offline operational checks using temporary databases and no MAX calls."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.backup import backup_before_change, create_backup, restore_backup, verify_backup
from app.config import load_sources, runtime_data_dir, validate_sources
from app.db import SCHEMA_VERSION, connect
from app.diagnostics import run_doctor


def source_config() -> dict:
    return {"version": 1, "sources": [{
        "id": "example", "name": "Example", "type": "official", "level": "primary",
        "url": "https://example.org", "allowed_domains": ["example.org"], "enabled": False,
        "retrieval": {"method": "manual", "endpoint": None, "verified": False},
    }]}


class ConfigValidationTests(unittest.TestCase):
    def test_repository_configuration_is_valid(self) -> None:
        self.assertEqual(len(load_sources()["sources"]), 8)

    def test_runtime_path_can_be_relocated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"APP_DATA_DIR": tmp}):
            self.assertEqual(runtime_data_dir(), Path(tmp).resolve())

    def test_invalid_disabled_sources_are_rejected_early(self) -> None:
        invalid_values = [
            ("enabled", "false"), ("allowed_domains", "example.org"),
            ("url", "https://user:password@example.org"), ("retrieval", {"method": "typo"}),
            ("relevance_filter", {"geography_terms": "not-a-list"}),
            ("enabledd", True),
        ]
        for field, value in invalid_values:
            with self.subTest(field=field):
                config = source_config()
                config["sources"][0][field] = value
                with self.assertRaises(ValueError):
                    validate_sources(config)

    def test_wrong_roots_duplicate_ids_and_unverified_automatic_sources_fail(self) -> None:
        for value in ([], {"sources": ["not-a-mapping"]}, {"sources": None}):
            with self.assertRaises(ValueError):
                validate_sources(value)
        config = source_config()
        config["sources"].append(dict(config["sources"][0]))
        with self.assertRaises(ValueError):
            validate_sources(config)
        config = source_config()
        config["sources"][0].update(enabled=True, retrieval={"method": "rss", "endpoint": "https://example.org/rss"})
        with self.assertRaisesRegex(ValueError, "verified"):
            validate_sources(config)

    def test_reliefweb_query_types_are_validated(self) -> None:
        config = source_config()
        config["sources"][0]["retrieval"] = {
            "method": "reliefweb_api", "endpoint": "https://api.reliefweb.int/v2/reports", "verified": True,
            "query": {"limit": "100"},
        }
        with self.assertRaises(ValueError):
            validate_sources(config)


class BackupTests(unittest.TestCase):
    def test_legacy_database_is_backed_up_before_migration_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as old:
                old.execute("""CREATE TABLE drafts(
                    id INTEGER PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,published_at TEXT,max_post_url TEXT)""")
                old.execute("INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES('legacy','retained','pending','old','old')")
                old.commit()
            with closing(connect(path)) as migrated:
                self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertIn("revision", {row[1] for row in migrated.execute("PRAGMA table_info(drafts)")})
                self.assertEqual(migrated.execute("SELECT body FROM drafts").fetchone()[0], "retained")
            backups = list((root / "backups").glob("*.sqlite3"))
            self.assertEqual(len(backups), 1)
            snapshot = backups[0]
            self.assertEqual(verify_backup(snapshot)["user_version"], 0)
            with closing(sqlite3.connect(snapshot)) as before:
                self.assertNotIn("revision", {row[1] for row in before.execute("PRAGMA table_info(drafts)")})
                self.assertEqual(before.execute("SELECT body FROM drafts").fetchone()[0], "retained")
                self.assertEqual(before.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 1)
            with patch("app.db._migrate_connection", side_effect=AssertionError("must not migrate again")):
                with closing(connect(path)):
                    pass
            self.assertEqual(list((root / "backups").glob("*.sqlite3")), backups)

    def test_failed_pre_migration_backup_preserves_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE evidence(value)")
            original = path.read_bytes()
            with patch("app.backup.backup_before_change", side_effect=OSError("backup unavailable")):
                with self.assertRaisesRegex(OSError, "backup unavailable"):
                    connect(path)
            self.assertEqual(path.read_bytes(), original)

    def test_new_database_has_current_version_without_unnecessary_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with closing(connect(root / "new.sqlite3")) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertFalse((root / "backups").exists())

    def test_newer_schema_is_rejected_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "future.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "более новой"):
                connect(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse((root / "backups").exists())

    def test_online_backup_includes_committed_wal_and_can_restore_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "live.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE evidence(id INTEGER PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO evidence(value) VALUES('preserved')")
                db.commit()
                result = create_backup(db, root / "snapshot.sqlite3")
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["foreign_keys_ok"])
                self.assertEqual(len(result["schema_sha256"]), 64)
            verified = verify_backup(Path(result["backup_path"]))
            restored = restore_backup(Path(result["backup_path"]), root / "restored.sqlite3")
            self.assertEqual(restored["sha256"], verified["sha256"])
            with closing(sqlite3.connect(restored["restored_path"])) as db:
                self.assertEqual(db.execute("SELECT value FROM evidence").fetchone()[0], "preserved")

    def test_backup_and_restore_never_overwrite_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "live.sqlite3"
            with closing(connect(path)):
                pass
            result = create_backup(path, root / "snapshot.sqlite3")
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                restore_backup(Path(result["backup_path"]), path)
            with self.assertRaises(FileExistsError):
                create_backup(path, Path(result["backup_path"]))
            with patch("app.config.DB_PATH", root / "future-current.sqlite3"):
                with self.assertRaises(FileExistsError):
                    restore_backup(Path(result["backup_path"]), root / "future-current.sqlite3")
            self.assertEqual(path.read_bytes(), before)

    def test_tampered_manifest_and_database_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "live.sqlite3"
            with closing(connect(path)):
                pass
            result = create_backup(path)
            manifest_path = Path(result["manifest_path"])
            original = manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(original)
            manifest["schema_sha256"] = "wrong"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Схема"):
                verify_backup(Path(result["backup_path"]))
            manifest_path.write_text(original, encoding="utf-8")
            with Path(result["backup_path"]).open("ab") as stream:
                stream.write(b"changed")
            with self.assertRaises(ValueError):
                restore_backup(Path(result["backup_path"]), root / "rejected.sqlite3")
            self.assertFalse((root / "rejected.sqlite3").exists())

    def test_foreign_key_violation_is_not_accepted_as_healthy_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "broken.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.executescript("CREATE TABLE parent(id PRIMARY KEY); CREATE TABLE child(id REFERENCES parent(id)); INSERT INTO child VALUES(7);")
            target = root / "bad-snapshot.sqlite3"
            with self.assertRaisesRegex(ValueError, "foreign_key_check"):
                create_backup(path, target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".sqlite3.manifest.json").exists())

    def test_backup_requires_finished_transaction_and_preserves_prechange_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "live.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE evidence(value)")
                db.execute("INSERT INTO evidence VALUES('before')")
                with self.assertRaisesRegex(ValueError, "транзакцию"):
                    create_backup(db, root / "unfinished.sqlite3")
                db.commit()
                result = backup_before_change(path, "test_migration")
                db.execute("UPDATE evidence SET value='after'")
                db.commit()
            self.assertEqual(result["reason"], "test_migration")
            with closing(sqlite3.connect(result["backup_path"])) as old:
                self.assertEqual(old.execute("SELECT value FROM evidence").fetchone()[0], "before")
            self.assertIsNone(backup_before_change(root / "absent.sqlite3"))


class DoctorTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        path = root / "sources.yaml"
        path.write_text(json.dumps(source_config()), encoding="utf-8")
        return path

    def test_doctor_is_offline_readonly_and_redacts_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "live.sqlite3"
            with closing(connect(path)) as db:
                db.execute("INSERT INTO drafts(title,body,status,created_at,updated_at) VALUES('test','test','pending','now','now')")
                db.execute("INSERT INTO publish_attempts(draft_id,attempt_no,state,created_at,details) VALUES(1,1,'unknown','now','private response')")
                db.commit()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch("app.credentials.get_max_credentials", return_value=("secret-token-value", "private-channel")), \
                    patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
                    patch("app.diagnostics._model_cache_present", return_value=False):
                result = run_doctor(path, self._config(root))
            checks = {row["name"]: row for row in result["checks"]}
            self.assertTrue(result["offline"])
            self.assertEqual(checks["database"]["status"], "ok")
            self.assertEqual(checks["schema"]["status"], "ok")
            self.assertEqual(checks["unresolved_attempts"]["count"], 1)
            output = json.dumps(result)
            for private in ("secret-token-value", "private-channel", "private response"):
                self.assertNotIn(private, output)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_doctor_does_not_create_or_migrate_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            with patch("app.credentials.get_max_credentials", return_value=(None, None)), \
                    patch("app.diagnostics._model_cache_present", return_value=False):
                missing = root / "missing" / "app.sqlite3"
                result = run_doctor(missing, config)
                self.assertFalse(missing.parent.exists())
                self.assertEqual(next(row for row in result["checks"] if row["name"] == "database")["status"], "warning")
                legacy = root / "legacy.sqlite3"
                with closing(sqlite3.connect(legacy)) as db:
                    db.execute("CREATE TABLE old_data(value)")
                before = legacy.read_bytes()
                result = run_doctor(legacy, config)
                self.assertEqual(next(row for row in result["checks"] if row["name"] == "schema")["status"], "error")
                self.assertEqual(legacy.read_bytes(), before)

    def test_credential_failure_does_not_echo_exception_or_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("app.credentials.get_max_credentials", side_effect=RuntimeError("private-secret")), \
                    patch("app.diagnostics._model_cache_present", return_value=False):
                result = run_doctor(root / "absent.sqlite3", self._config(root))
            self.assertNotIn("private-secret", json.dumps(result))
            self.assertEqual(next(row for row in result["checks"] if row["name"] == "max_credentials")["status"], "error")

    def test_doctor_detects_removed_workflow_guard_without_repairing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "damaged-schema.sqlite3"
            with closing(connect(path)) as db:
                db.execute("DROP TRIGGER workflow_draft_revision")
                db.commit()
            original = path.read_bytes()
            with patch("app.credentials.get_max_credentials", return_value=(None, None)), \
                    patch("app.diagnostics._model_cache_present", return_value=False):
                result = run_doctor(path, self._config(root))
            schema = next(row for row in result["checks"] if row["name"] == "schema")
            self.assertEqual(schema["status"], "error")
            self.assertIn("workflow_draft_revision", schema["missing_workflow_guards"])
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
