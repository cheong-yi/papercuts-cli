import configparser
import email.parser
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
VALID_EVENTS = (FIXTURES / "valid-events.jsonl").read_bytes()
MALFORMED_EVENTS = (FIXTURES / "malformed-tail.jsonl").read_bytes()


def _base_environment():
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH"):
        environment.pop(name, None)
    environment.update(
        {
            "LC_ALL": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return environment


def _git_repository(root):
    completed = subprocess.run(
        ["git", "init", "-q"],
        cwd=root,
        env=_base_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)


def _install_ledger(repo, contents):
    storage = repo / ".git" / "papercuts"
    storage.mkdir(mode=0o700)
    storage.chmod(0o700)
    lock = storage / "write.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    ledger = storage / "events.jsonl"
    ledger.write_bytes(contents)
    ledger.chmod(0o600)
    return storage


class PackagingMetadataTests(unittest.TestCase):
    def test_pep517_metadata_discovers_src_package_and_console_script(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            metadata["build-system"],
            {
                "requires": ["setuptools>=61"],
                "build-backend": "setuptools.build_meta",
            },
        )
        self.assertEqual(metadata["project"]["readme"], "README.md")
        self.assertEqual(metadata["project"]["requires-python"], ">=3.11")
        self.assertEqual(metadata["project"]["dependencies"], [])
        self.assertEqual(
            metadata["project"]["scripts"],
            {"papercut": "papercuts.cli:main"},
        )
        self.assertEqual(
            metadata["tool"]["setuptools"]["package-dir"],
            {"": "src"},
        )
        self.assertEqual(
            metadata["tool"]["setuptools"]["packages"]["find"]["where"],
            ["src"],
        )

    def test_readme_documents_pinned_isolated_package_lifecycle(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.split())

        for heading in (
            "## Isolated installation",
            "### Upgrade",
            "### Rollback",
            "### Uninstall",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, readme)
        self.assertIn("EXPECTED_COMMIT", readme)
        self.assertIn("sha256sum", readme)
        self.assertIn("--no-index --no-deps", normalized)
        self.assertIn('"$INSTALL_ROOT/bin/papercut" --help', readme)
        self.assertIn("not published to a package index", normalized)
        self.assertIn("does not download or resolve", normalized)
        self.assertIn("without changing a user or system `PATH`", normalized)
        self.assertIn("set -eu\nSOURCE_ROOT=", readme)
        self.assertIn(
            'git -C "$SOURCE_ROOT" archive --format=tar "$EXPECTED_COMMIT"',
            readme,
        )
        self.assertNotIn('cp "$SOURCE_ROOT/pyproject.toml"', readme)
        self.assertNotIn('cp -R "$SOURCE_ROOT/src"', readme)
        self.assertIn('test ! -e "$BUILD_ROOT"', readme)
        self.assertIn('test ! -e "$INSTALL_ROOT"', readme)
        self.assertGreaterEqual(readme.count("env -u PYTHONPATH"), 3)
        self.assertIn(
            '--force-reinstall --upgrade "$NEXT_WHEEL"',
            normalized,
        )
        self.assertIn("provides package metadata", normalized)
        for excluded_claim in (
            "package-index publication",
            "live user or system installation or adoption",
            "`PATH` or profile mutation",
            "automatic enablement",
        ):
            with self.subTest(excluded_claim=excluded_claim):
                self.assertIn(excluded_claim, normalized)


class BuiltDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(
            prefix="papercuts-packaging-",
            dir="/tmp",
        )
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.root = Path(cls._temporary.name)
        cls.project = cls.root / "source"
        cls.dist = cls.project / "dist"
        cls.home = cls.root / "home"
        cls.tmp = cls.root / "tmp"
        cls.project.mkdir()
        cls.home.mkdir()
        cls.tmp.mkdir()
        shutil.copytree(ROOT / "src", cls.project / "src")
        shutil.copytree(ROOT / "tests", cls.project / "tests")
        for name in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "MANIFEST.in",
            ".gitignore",
            "PUBLIC_FILES.json",
        ):
            shutil.copy2(ROOT / name, cls.project / name)
        cls.dist.mkdir()

        environment = _base_environment()
        environment.update(
            {
                "HOME": str(cls.home),
                "TMPDIR": str(cls.tmp),
            }
        )
        backend_build = (
            "import setuptools.build_meta as backend; "
            "backend.build_wheel('dist'); "
            "backend.build_sdist('dist')"
        )
        completed = subprocess.run(
            [sys.executable, "-B", "-c", backend_build],
            cwd=cls.project,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(
                "offline backend build failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        wheels = list(cls.dist.glob("*.whl"))
        sdists = list(cls.dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            names = sorted(path.name for path in cls.dist.iterdir())
            raise AssertionError(f"unexpected artifacts: {names}")
        cls.wheel = wheels[0]
        cls.sdist = sdists[0]

        cls.venv = cls.root / "venv"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "venv",
                "--without-pip",
                str(cls.venv),
            ],
            cwd=cls.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(
                "disposable venv creation failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        cls.installed_command = cls.venv / "bin" / "papercut"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "pip",
                "--python",
                str(cls.venv / "bin" / "python"),
                "install",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                "--no-cache-dir",
                str(cls.wheel),
            ],
            cwd=cls.root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(
                "offline disposable wheel install failed\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        if not cls.installed_command.is_file():
            raise AssertionError("wheel install did not generate bin/papercut")

    @classmethod
    def _run_source(cls, cwd, *args):
        environment = _base_environment()
        environment["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(
            [sys.executable, "-B", "-m", "papercuts", *args],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    @classmethod
    def _run_installed(cls, cwd, *args):
        environment = _base_environment()
        self_path = str(cls.installed_command)
        return subprocess.run(
            [self_path, *args],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assertSameResult(self, source, installed):
        self.assertEqual(
            (installed.returncode, installed.stdout, installed.stderr),
            (source.returncode, source.stdout, source.stderr),
        )

    def test_wheel_metadata_contents_and_entry_point_are_bounded(self):
        expected_modules = {
            f"papercuts/{path.name}"
            for path in (ROOT / "src" / "papercuts").glob("*.py")
        }
        allowed_metadata = {
            "METADATA",
            "WHEEL",
            "entry_points.txt",
            "top_level.txt",
            "RECORD",
            "LICENSE",
        }
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
            self.assertLessEqual(expected_modules, names)
            metadata_names = {
                name for name in names if ".dist-info/" in name
            }
            self.assertEqual(names, expected_modules | metadata_names)
            self.assertTrue(metadata_names)
            self.assertTrue(
                all(
                    name.rsplit("/", 1)[-1] in allowed_metadata
                    for name in metadata_names
                ),
                metadata_names,
            )
            metadata_name = next(
                name for name in names if name.endswith(".dist-info/METADATA")
            )
            entry_points_name = next(
                name
                for name in names
                if name.endswith(".dist-info/entry_points.txt")
            )
            metadata = email.parser.BytesParser().parsebytes(
                archive.read(metadata_name)
            )
            entry_points = configparser.ConfigParser()
            entry_points.read_string(
                archive.read(entry_points_name).decode("utf-8")
            )

        self.assertEqual(metadata["Name"], "papercuts-cli")
        self.assertEqual(metadata["Version"], "0.0.0")
        self.assertEqual(metadata["Requires-Python"], ">=3.11")
        self.assertIsNone(metadata.get_all("Requires-Dist"))
        self.assertEqual(
            dict(entry_points["console_scripts"]),
            {"papercut": "papercuts.cli:main"},
        )
        self.assertFalse(
            any(
                part in {".git", ".env", "__pycache__", "tests", "papercuts"}
                and name.startswith(("/", "../"))
                for name in names
                for part in name.split("/")
            )
        )

    def test_sdist_contains_only_package_and_build_inputs(self):
        expected_modules = {
            f"src/papercuts/{path.name}"
            for path in (ROOT / "src" / "papercuts").glob("*.py")
        }
        allowed_exact = {
            ".gitignore",
            "LICENSE",
            "MANIFEST.in",
            "PUBLIC_FILES.json",
            "PKG-INFO",
            "README.md",
            "pyproject.toml",
            "setup.cfg",
            *expected_modules,
        }
        allowed_egg_info = {
            "PKG-INFO",
            "SOURCES.txt",
            "dependency_links.txt",
            "entry_points.txt",
            "top_level.txt",
        }
        with tarfile.open(self.sdist, "r:gz") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]

        relative_names = {
            member.name.split("/", 1)[1]
            for member in members
            if "/" in member.name
        }
        unexpected = set()
        for name in relative_names:
            if name in allowed_exact:
                continue
            prefix = "src/papercuts_cli.egg-info/"
            if name.startswith(prefix) and name.removeprefix(prefix) in allowed_egg_info:
                continue
            unexpected.add(name)

        self.assertFalse(unexpected)
        self.assertLessEqual(allowed_exact, relative_names)
        self.assertFalse(
            any(
                name.startswith(("/", "../"))
                or "/.git/" in f"/{name}/"
                or "/tests/" in f"/{name}/"
                or "__pycache__" in name
                or name.endswith(".pyc")
                for name in relative_names
            )
        )

    def test_generated_command_help_exactly_matches_source(self):
        source = self._run_source(self.root, "--help")
        installed = self._run_installed(self.root, "--help")

        self.assertSameResult(source, installed)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(installed.stderr, "")
        self.assertIn("usage: papercut", installed.stdout)
        for command in ("record", "resolve", "list", "render", "check", "packet"):
            with self.subTest(command=command):
                self.assertIn(command, installed.stdout)

    def test_deterministic_list_and_packet_match_without_side_effects(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            repo = Path(directory)
            _git_repository(repo)
            storage = _install_ledger(repo, VALID_EVENTS)
            before = {
                entry.name: (
                    entry.read_bytes(),
                    stat.S_IMODE(entry.stat().st_mode),
                )
                for entry in storage.iterdir()
            }

            for args in (
                ("list", "--status", "all", "--limit", "25"),
                ("packet", "--expect-head", "unborn"),
            ):
                with self.subTest(args=args):
                    source = self._run_source(repo, *args)
                    installed = self._run_installed(repo, *args)
                    self.assertSameResult(source, installed)
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    self.assertEqual(installed.stderr, "")

            after = {
                entry.name: (
                    entry.read_bytes(),
                    stat.S_IMODE(entry.stat().st_mode),
                )
                for entry in storage.iterdir()
            }
            self.assertEqual(after, before)

    def test_resolve_render_and_check_match_source_behavior(self):
        open_record_id = "00000000-0000-4000-8000-000000000102"
        resolution = "packaging parity verified"
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            fixture_root = Path(directory)
            source_repo = fixture_root / "source-repo"
            installed_repo = fixture_root / "installed-repo"
            source_repo.mkdir()
            installed_repo.mkdir()
            for repo in (source_repo, installed_repo):
                _git_repository(repo)
                _install_ledger(repo, VALID_EVENTS)

            source_resolve = self._run_source(
                source_repo,
                "resolve",
                open_record_id,
                "--resolution",
                resolution,
            )
            installed_resolve = self._run_installed(
                installed_repo,
                "resolve",
                open_record_id,
                "--resolution",
                resolution,
            )

            self.assertSameResult(source_resolve, installed_resolve)
            self.assertEqual(
                (
                    installed_resolve.returncode,
                    installed_resolve.stdout,
                    installed_resolve.stderr,
                ),
                (0, f"resolved {open_record_id}\n", ""),
            )

            ledgers = []
            for repo in (source_repo, installed_repo):
                ledger = repo / ".git" / "papercuts" / "events.jsonl"
                events = [
                    json.loads(line)
                    for line in ledger.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(len(events), 4)
                appended = events[-1]
                self.assertEqual(
                    set(appended),
                    {
                        "schema_version",
                        "event_id",
                        "event_type",
                        "occurred_at",
                        "record_id",
                        "resolution",
                    },
                )
                event_id = uuid.UUID(appended["event_id"])
                self.assertEqual(event_id.version, 4)
                self.assertEqual(str(event_id), appended["event_id"])
                self.assertIsNotNone(
                    re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
                        appended["occurred_at"],
                    )
                )
                normalized = dict(appended)
                normalized["event_id"] = "<uuid4>"
                normalized["occurred_at"] = "<timestamp>"
                ledgers.append((events[:-1], normalized))

            self.assertEqual(ledgers[0], ledgers[1])
            before_reads = {
                repo: {
                    entry.name: (
                        entry.read_bytes(),
                        stat.S_IMODE(entry.stat().st_mode),
                    )
                    for entry in (repo / ".git" / "papercuts").iterdir()
                }
                for repo in (source_repo, installed_repo)
            }

            for args in (("render", "--status", "all"), ("check",)):
                with self.subTest(args=args):
                    source = self._run_source(source_repo, *args)
                    installed = self._run_installed(installed_repo, *args)
                    self.assertSameResult(source, installed)
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    self.assertEqual(installed.stderr, "")
                    if args == ("check",):
                        self.assertEqual(
                            installed.stdout,
                            "ok: 4 events, 2 records\n",
                        )
                    else:
                        self.assertIn(
                            f"## {open_record_id} | resolved | tooling",
                            installed.stdout,
                        )
                        self.assertIn(
                            f"resolution={json.dumps(resolution)}",
                            installed.stdout,
                        )

            after_reads = {
                repo: {
                    entry.name: (
                        entry.read_bytes(),
                        stat.S_IMODE(entry.stat().st_mode),
                    )
                    for entry in (repo / ".git" / "papercuts").iterdir()
                }
                for repo in (source_repo, installed_repo)
            }
            self.assertEqual(after_reads, before_reads)

    def test_absent_ledger_list_matches_and_creates_nothing(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            repo = Path(directory)
            _git_repository(repo)

            source = self._run_source(repo, "list")
            installed = self._run_installed(repo, "list")

            self.assertSameResult(source, installed)
            self.assertEqual(
                (installed.returncode, installed.stdout, installed.stderr),
                (0, "", ""),
            )
            self.assertFalse((repo / ".git" / "papercuts").exists())

    def test_capture_and_immediate_list_work_at_root_and_nested_directory(self):
        uuid_pattern = re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\n"
        )
        runners = (
            ("source", self._run_source),
            ("installed", self._run_installed),
        )
        for runner_name, runner in runners:
            for nested in (False, True):
                with self.subTest(runner=runner_name, nested=nested):
                    with tempfile.TemporaryDirectory(dir=self.root) as directory:
                        repo = Path(directory)
                        _git_repository(repo)
                        cwd = repo
                        if nested:
                            cwd = repo / "nested" / "directory"
                            cwd.mkdir(parents=True)

                        captured = runner(
                            cwd,
                            "record",
                            "--summary",
                            f"{runner_name} capture",
                        )

                        self.assertEqual(captured.returncode, 0, captured.stderr)
                        self.assertEqual(captured.stderr, "")
                        self.assertIsNotNone(uuid_pattern.fullmatch(captured.stdout))
                        record_id = captured.stdout.strip()
                        listed = runner(cwd, "list", "--status", "all")
                        self.assertEqual(
                            (listed.returncode, listed.stderr),
                            (0, ""),
                        )
                        self.assertIn(record_id, listed.stdout)
                        ledger = repo / ".git" / "papercuts" / "events.jsonl"
                        events = [
                            json.loads(line)
                            for line in ledger.read_text(encoding="utf-8").splitlines()
                        ]
                        self.assertEqual(len(events), 1)
                        self.assertEqual(events[0]["record_id"], record_id)
                        self.assertIsNone(events[0]["category"])
                        self.assertIsNone(events[0]["expected"])
                        self.assertIsNone(events[0]["observed"])
                        self.assertIsNone(events[0]["evidence_basis"])
                        self.assertIsNone(events[0]["recurrence_key"])

    def test_argument_negative_boundaries_exactly_match(self):
        for args in ((), ("list", "--limit", "0"), ("list", "--limit", "26")):
            with self.subTest(args=args):
                source = self._run_source(self.root, *args)
                installed = self._run_installed(self.root, *args)
                self.assertSameResult(source, installed)
                self.assertEqual(
                    (installed.returncode, installed.stdout, installed.stderr),
                    (2, "", "papercuts: E_ARGUMENT\n"),
                )

    def test_repository_privacy_and_malformed_failures_match_without_writes(self):
        source_outside = self._run_source(self.root, "list")
        installed_outside = self._run_installed(self.root, "list")
        self.assertSameResult(source_outside, installed_outside)
        self.assertEqual(installed_outside.returncode, 1)
        self.assertEqual(installed_outside.stdout, "")

        for runner_name, runner in (
            ("source", self._run_source),
            ("installed", self._run_installed),
        ):
            with self.subTest(boundary="privacy", runner=runner_name):
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    repo = Path(directory)
                    _git_repository(repo)
                    result = runner(
                        repo,
                        "record",
                        "--summary",
                        "/" + "home/alice/private",
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr,
                        "papercuts: E_PRIVACY_HOME_PATH field=summary\n",
                    )
                    self.assertFalse((repo / ".git" / "papercuts").exists())

            with self.subTest(boundary="malformed", runner=runner_name):
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    repo = Path(directory)
                    _git_repository(repo)
                    storage = _install_ledger(repo, MALFORMED_EVENTS)
                    before = {
                        entry.name: entry.read_bytes()
                        for entry in storage.iterdir()
                    }
                    result = runner(repo, "list")
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("papercuts: E_", result.stderr)
                    after = {
                        entry.name: entry.read_bytes()
                        for entry in storage.iterdir()
                    }
                    self.assertEqual(after, before)

    def test_wrong_packet_head_and_output_ceiling_exactly_match(self):
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            repo = Path(directory)
            _git_repository(repo)
            storage = _install_ledger(repo, VALID_EVENTS)
            before = {
                entry.name: entry.read_bytes()
                for entry in storage.iterdir()
            }
            args = ("packet", "--expect-head", "0" * 40)
            source = self._run_source(repo, *args)
            installed = self._run_installed(repo, *args)
            self.assertSameResult(source, installed)
            self.assertEqual(
                (installed.returncode, installed.stdout, installed.stderr),
                (2, "", "papercuts: E_PACKET_WRONG_HEAD\n"),
            )
            after = {
                entry.name: entry.read_bytes()
                for entry in storage.iterdir()
            }
            self.assertEqual(after, before)

        events = []
        for number in range(1, 26):
            events.append(
                {
                    "category": None,
                    "event_id": str(uuid.UUID(int=number, version=4)),
                    "event_type": "recorded",
                    "evidence_basis": None,
                    "expected": None,
                    "observed": None,
                    "occurred_at": "2026-01-01T00:00:00.000000Z",
                    "record_id": str(uuid.UUID(int=number + 100, version=4)),
                    "recurrence_key": None,
                    "schema_version": 1,
                    "summary": "x" * 120,
                }
            )
        contents = b"".join(
            json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
            for event in events
        )
        selected_bytes = sum(
            len(
                (
                    f"{event['record_id']}\topen\tnull\t"
                    f"{json.dumps(event['summary'])}\n"
                ).encode("utf-8")
            )
            for event in events
        )
        self.assertGreater(selected_bytes, 4096)
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            repo = Path(directory)
            _git_repository(repo)
            storage = _install_ledger(repo, contents)
            before = {
                entry.name: entry.read_bytes()
                for entry in storage.iterdir()
            }
            args = ("list", "--status", "all", "--limit", "25")
            source = self._run_source(repo, *args)
            installed = self._run_installed(repo, *args)
            self.assertSameResult(source, installed)
            self.assertEqual(
                (installed.returncode, installed.stdout, installed.stderr),
                (1, "", "papercuts: E_OUTPUT_LIMIT\n"),
            )
            after = {
                entry.name: entry.read_bytes()
                for entry in storage.iterdir()
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
