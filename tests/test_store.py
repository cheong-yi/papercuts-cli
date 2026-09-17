import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

from papercuts import repo as repo_module
from papercuts.model import LedgerError
from papercuts.repo import StorageError, discover_repository, read_storage
from papercuts.store import check, record, resolve, snapshot


class StoreTests(unittest.TestCase):
    def _repo(self):
        td = tempfile.TemporaryDirectory()
        root = pathlib.Path(td.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return td, pathlib.Path(discover_repository(root).common_dir)

    def _record(self, common, summary="one"):
        return record(str(common), category="docs", summary=summary, expected="yes", observed="no", evidence_basis="test")

    def test_lifecycle_has_two_canonical_events_and_one_reduced_record(self):
        td, common = self._repo()
        with td:
            record_id = self._record(common)
            self.assertEqual(snapshot(str(common))[0].record_id, record_id)
            resolve(str(common), record_id, resolution="fixed")
            events = read_storage(str(common))
            self.assertEqual([event["event_type"] for event in events], ["recorded", "resolved"])
            self.assertEqual(check(str(common)), (2, 1))
            self.assertEqual(snapshot(str(common))[0].status, "resolved")

    def test_compact_record_persists_explicit_nulls_and_reduces_them(self):
        td, common = self._repo()
        with td:
            record_id = record(str(common), summary="brief friction")

            [event] = read_storage(str(common))
            self.assertEqual(event["record_id"], record_id)
            self.assertEqual(event["summary"], "brief friction")
            for field in (
                "category",
                "expected",
                "observed",
                "evidence_basis",
                "recurrence_key",
            ):
                self.assertIn(field, event)
                self.assertIsNone(event[field])

            [reduced] = snapshot(str(common))
            self.assertEqual(reduced.record_id, record_id)
            self.assertIsNone(reduced.category)
            self.assertIsNone(reduced.expected)
            self.assertIsNone(reduced.observed)
            self.assertIsNone(reduced.evidence_basis)
            self.assertIsNone(reduced.recurrence_key)

    def test_unknown_resolve_does_not_create_ledger_or_write(self):
        td, common = self._repo()
        with td:
            with mock.patch("papercuts.store.os.write", side_effect=AssertionError("write invoked")):
                with self.assertRaisesRegex(LedgerError, "E_UNKNOWN_RECORD"):
                    resolve(str(common), "00000000-0000-4000-8000-000000000000", resolution="fixed")
            storage = common / "papercuts"
            self.assertTrue(storage.is_dir())
            self.assertFalse((storage / "events.jsonl").exists())

    def test_duplicate_resolve_preserves_ledger_bytes(self):
        td, common = self._repo()
        with td:
            record_id = self._record(common)
            resolve(str(common), record_id, resolution="fixed")
            ledger = common / "papercuts" / "events.jsonl"
            before = ledger.read_bytes()
            with self.assertRaisesRegex(LedgerError, "E_DUPLICATE_RESOLUTION"):
                resolve(str(common), record_id, resolution="again")
            self.assertEqual(ledger.read_bytes(), before)

    def test_malformed_history_blocks_read_and_append(self):
        td, common = self._repo()
        with td:
            self._record(common)
            ledger = common / "papercuts" / "events.jsonl"
            before = ledger.read_bytes()
            ledger.write_bytes(before[:-1])
            with self.assertRaisesRegex(LedgerError, "E_MISSING_FINAL_LF"):
                snapshot(str(common))
            with self.assertRaisesRegex(LedgerError, "E_MISSING_FINAL_LF"):
                self._record(common, summary="blocked")
            self.assertEqual(ledger.read_bytes(), before[:-1])

    def test_prewrite_storage_directory_fsync_failure_is_determinate(self):
        td, common = self._repo()
        with td:
            storage = common / "papercuts"
            ledger = storage / "events.jsonl"
            real_fsync = os.fsync
            fired = False

            def fail_before_data_write(fd):
                nonlocal fired
                if not fired and ledger.exists() and ledger.stat().st_size == 0:
                    fired = True
                    raise OSError("injected pre-write directory fsync failure")
                return real_fsync(fd)

            with mock.patch.object(repo_module.os, "fsync", side_effect=fail_before_data_write):
                with mock.patch.object(repo_module.os, "write", side_effect=AssertionError("write invoked")):
                    with self.assertRaisesRegex(StorageError, "E_STORAGE") as caught:
                        self._record(common)
            self.assertTrue(fired)
            self.assertEqual(caught.exception.rule_id, "E_STORAGE")
            self.assertFalse(ledger.read_bytes())

    def test_opened_storage_directory_replacement_fails_closed(self):
        td, common = self._repo()
        with td:
            real_open = os.open
            replaced = False
            original = common / "papercuts-original"
            storage = common / "papercuts"

            def replace_after_open(path, flags, *args, **kwargs):
                nonlocal replaced
                fd = real_open(path, flags, *args, **kwargs)
                if path == "papercuts" and kwargs.get("dir_fd") is not None and not replaced:
                    replaced = True
                    os.rename(storage, original)
                    os.mkdir(storage, 0o700)
                return fd

            with mock.patch.object(repo_module.os, "open", side_effect=replace_after_open):
                with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE") as caught:
                    self._record(common)
            self.assertTrue(replaced)
            self.assertEqual(caught.exception.rule_id, "E_STORAGE_UNSAFE")
            self.assertFalse((storage / "write.lock").exists())
            self.assertFalse((storage / "events.jsonl").exists())

    def test_write_failure_is_indeterminate_and_not_retried(self):
        td, common = self._repo()
        with td:
            fired = mock.Mock(side_effect=OSError("injected"))
            with mock.patch("papercuts.store.os.write", fired):
                with self.assertRaisesRegex(StorageError, "E_STORAGE_INDETERMINATE"):
                    self._record(common)
            fired.assert_called_once()

    def test_existing_ledger_append_is_independent_of_retained_offset(self):
        td, common = self._repo()
        with td:
            first_id = self._record(common, summary="before")
            real_write = os.write
            rewound = False

            def rewind_retained_write(fd, data):
                nonlocal rewound
                if not rewound:
                    rewound = True
                    os.lseek(fd, 0, os.SEEK_SET)
                return real_write(fd, data)

            with mock.patch("papercuts.repo.os.write", side_effect=rewind_retained_write):
                second_id = self._record(common, summary="after")
            self.assertTrue(rewound)
            events = read_storage(str(common))
            self.assertEqual([event["record_id"] for event in events], [first_id, second_id])
            self.assertEqual([row.summary for row in snapshot(str(common))], ["before", "after"])

    def test_canonical_replacement_during_append_is_indeterminate(self):
        td, common = self._repo()
        with td:
            self._record(common, summary="before")
            ledger = common / "papercuts" / "events.jsonl"
            before = ledger.read_bytes()
            real_write = os.write
            replaced = False

            def replace_before_retained_write(fd, data):
                nonlocal replaced
                if not replaced:
                    replaced = True
                    ledger.unlink()
                    ledger.write_bytes(before)
                    os.chmod(ledger, 0o600)
                return real_write(fd, data)

            with mock.patch("papercuts.repo.os.write", side_effect=replace_before_retained_write):
                with self.assertRaisesRegex(StorageError, "E_STORAGE_INDETERMINATE"):
                    self._record(common, summary="after")
            self.assertTrue(replaced)
            self.assertEqual(ledger.read_bytes(), before)
            self.assertNotIn(b'"summary":"after"', ledger.read_bytes())


if __name__ == "__main__":
    unittest.main()
