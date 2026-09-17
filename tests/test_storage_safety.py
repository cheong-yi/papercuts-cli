import os
from pathlib import Path
import stat
import tempfile
import unittest

from papercuts.repo import StorageError, read_storage
from papercuts.store import check, record, snapshot


class StorageSafetyTests(unittest.TestCase):
    def _common(self, root, state):
        common = root / "common"
        common.mkdir()
        if state != "absent":
            storage = common / "papercuts"
            storage.mkdir(mode=0o700)
            if state in {"lock", "both"}:
                (storage / "write.lock").touch(mode=0o600)
            if state in {"ledger", "both"}:
                (storage / "events.jsonl").touch(mode=0o600)
        return common

    def test_absent_empty_and_lock_only_readers_create_nothing(self):
        for state in ("absent", "empty", "lock"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                common = self._common(Path(directory), state)
                storage = common / "papercuts"
                before = sorted(path.name for path in storage.iterdir()) if storage.exists() else []
                self.assertEqual(read_storage(common), [])
                self.assertEqual(snapshot(str(common)), [])
                self.assertEqual(check(str(common)), (0, 0))
                after = sorted(path.name for path in storage.iterdir()) if storage.exists() else []
                self.assertEqual(after, before)

    def test_ledger_without_lock_fails_closed_for_all_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            common = self._common(Path(directory), "ledger")
            operations = (
                lambda: read_storage(common),
                lambda: snapshot(str(common)),
                lambda: check(str(common)),
            )
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaisesRegex(
                    StorageError, "E_STORAGE_LEDGER_WITHOUT_LOCK"
                ):
                    operation()

    def test_unsafe_ledger_mode_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = self._common(root, "both")
            ledger = common / "papercuts" / "events.jsonl"
            ledger.chmod(0o644)
            with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
                check(str(common))
            ledger.chmod(0o600)
            ledger.unlink()
            os.symlink(root / "missing", ledger)
            with self.assertRaisesRegex(StorageError, "E_STORAGE_UNSAFE"):
                snapshot(str(common))

    def test_writer_creates_private_storage_lock_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            common = self._common(Path(directory), "empty")
            record(
                str(common),
                category="docs",
                summary="x",
                expected="y",
                observed="z",
                evidence_basis="test",
            )
            storage = common / "papercuts"
            self.assertEqual(stat.S_IMODE(storage.stat().st_mode), 0o700)
            for name in ("write.lock", "events.jsonl"):
                with self.subTest(name=name):
                    entry = storage / name
                    self.assertTrue(entry.is_file())
                    self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
