# Papercuts CLI

![A hand-drawn robot archivist reviews and resolves a friction note between the Papercuts terminal and a local ledger](docs/assets/readme/papercuts-ledger-hero.png)

> A local-first, append-only ledger for small developer and AI-agent workflow friction.

Capture an observation, inspect it, and record a resolution without editing project files or granting authority.

## Quickstart

Requires Python 3.11+, Git, and a local Linux filesystem. From a reviewed checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/papercuts record --summary 'editor opens slowly'
.venv/bin/papercuts list
.venv/bin/papercuts resolve <id> --resolution 'disabled the slow extension'
.venv/bin/papercuts check
```

Replace `<id>` with the ID returned by `record`. Installation can download build tooling, so it is not universally offline. Runtime uses only the Python standard library and Git; it makes no network calls and sends no telemetry. To run from the checkout without installing:

```sh
PYTHONPATH=src python3 -B -m papercuts --help
```

## Commands and writes

- `record --summary TEXT`: append a friction record. Optional `--category`, `--expected`, `--observed`, `--evidence-basis`, and `--recurrence-key` add context.
- `resolve ID --resolution TEXT`: append one resolution for an existing record; a record can be resolved once.
- `list [--status open|resolved|all] [--limit N]`: print records in first-recorded order; defaults to open records and 5 rows, with a maximum of 25.
- `render [--status open|resolved|all]`: print deterministic, inert Markdown; it is never saved automatically.
- `check`: validate the ledger without changing it.
- `packet --expect-head OID|unborn [--record-id UUID4 ... | --event-id UUID4 ...]`: print canonical JSON bound to the expected repository HEAD. Selectors are repeatable, unique, limited to 25, and cannot be mixed.

Run `papercuts COMMAND --help` for accepted values. Only `record` and `resolve` write ledger events; `list`, `render`, `check`, and `packet` are read-only.

## Boundaries and safety

A record is an observation, not a signature, approval, instruction, permission, or proof. A packet does not authorize action. Capture and resolution are manual: there are no hooks, daemons, automatic transcript collection, integrations, automatic fixes, or automatic enablement.

Events are stored at `<git-common-dir>/papercuts/events.jsonl`; linked worktrees share one ledger. The storage directory and files are owner-only. Readers use shared locks and writers use exclusive locks. Unsafe storage, lock contention, or malformed history fail closed rather than repairing data.

The ledger is bounded to 8 MiB, 10,000 events, and 8,192 bytes per event line. List output is bounded to 4,096 UTF-8 bytes. Papercuts is not a general-purpose log collector or issue tracker. Network filesystems and non-Linux platforms are unsupported.

Before writing, Papercuts rejects selected home/drive/UNC paths, URI credentials, private-key headers, specific GitHub/OpenAI-style/AWS/Slack token shapes, and selected Unicode controls. This is a narrow pattern filter, **not general secret detection**; accepted text is not guaranteed safe to share.

Paraphrase sensitive material. Arguments may appear in shell history or process listings; output may appear in terminal scrollback, redirected files, or logs. Review inputs and output destinations yourself. Ledger files are local runtime data, not publication material. `.gitignore` rules are a precaution, not a security boundary.

## Isolated installation

Papercuts is not published to a package index. For pinned evaluation without changing a user or system `PATH`, use an exact reviewed commit and disposable directories:

```sh
set -eu
SOURCE_ROOT=/path/to/reviewed/papercuts-cli
EXPECTED_COMMIT=0123456789abcdef0123456789abcdef01234567
BUILD_ROOT=/tmp/papercuts-build-"$EXPECTED_COMMIT"
INSTALL_ROOT=/tmp/papercuts-install-"$EXPECTED_COMMIT"
test "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" = "$EXPECTED_COMMIT"
test ! -e "$BUILD_ROOT"
test ! -e "$INSTALL_ROOT"
mkdir -p "$BUILD_ROOT/source" "$BUILD_ROOT/dist"
git -C "$SOURCE_ROOT" archive --format=tar "$EXPECTED_COMMIT" > "$BUILD_ROOT/source.tar"
tar -xf "$BUILD_ROOT/source.tar" -C "$BUILD_ROOT/source"
(
  cd "$BUILD_ROOT/source"
  env -u PYTHONPATH python3 -B -c \
    'import setuptools.build_meta as backend; backend.build_wheel("../dist")'
)
WHEEL="$BUILD_ROOT/dist/papercuts_cli-0.0.0-py3-none-any.whl"
sha256sum "$WHEEL"
env -u PYTHONPATH python3 -m venv --without-pip "$INSTALL_ROOT"
env -u PYTHONPATH python3 -m pip --python "$INSTALL_ROOT/bin/python" install \
  --no-index --no-deps --disable-pip-version-check "$WHEEL"
env -u PYTHONPATH "$INSTALL_ROOT/bin/papercuts" --help
```

Replace the example commit ID. This route requires locally available `setuptools>=61`, wheel, venv, and pip tooling; it does not download or resolve build dependencies. Retain the reviewed wheel and its SHA-256.

### Upgrade

Build another reviewed commit in separate disposable directories, then install its wheel. The version is currently `0.0.0`, so force replacement when evaluating another revision:

```sh
NEXT_WHEEL=/path/to/reviewed/next.whl
env -u PYTHONPATH python3 -m pip --python "$INSTALL_ROOT/bin/python" install \
  --no-index --no-deps --force-reinstall --upgrade "$NEXT_WHEEL"
```

### Rollback

Reinstall the retained prior wheel:

```sh
env -u PYTHONPATH python3 -m pip --python "$INSTALL_ROOT/bin/python" install \
  --no-index --no-deps --force-reinstall "$WHEEL"
```

### Uninstall

```sh
env -u PYTHONPATH python3 -m pip --python "$INSTALL_ROOT/bin/python" uninstall -y papercuts-cli
```

Uninstalling does not remove repository ledger data. This repository provides package metadata and evaluation instructions, not evidence of package-index publication, live user or system installation or adoption, `PATH` or profile mutation, or automatic enablement.

## Verification and source

Run the full test suite from the checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

Tests cover storage safety, privacy boundaries, lifecycle commands, packets, rendering, concurrency, no-network behavior, and packaging. This repository does not provide CI configuration.

[`PUBLIC_FILES.json`](PUBLIC_FILES.json) is the closed-world source inventory: its sorted `files` array must equal every regular public file in the checkout, excluding `.git`; every other file has a SHA-256 in `sha256`, while the manifest omits its own hash to avoid self-reference.

- [`src/papercuts/cli.py`](src/papercuts/cli.py) — command surface and output behavior.
- [`src/papercuts/privacy.py`](src/papercuts/privacy.py) — caller-input privacy filter.
- [`src/papercuts/repo.py`](src/papercuts/repo.py) — Git identity, locks, and storage safety.
- [`tests/`](tests/) — regression and packaging coverage.

## Attribution

Conceptual inspiration: [Steve Ruiz's papercuts thread](https://x.com/i/status/2075303919664734295) and [aurorascharff/agent-friction-skill](https://github.com/aurorascharff/agent-friction-skill/commit/945d109afadd5dfbd3dbff46992129c17ec7b098).

Papercuts is an independent implementation. Inspection of this source found no imported source code or runtime dependency from either reference; the attribution is for the concept, not code reuse or endorsement.

## License

MIT © 2026 cheong-yi. See [LICENSE](LICENSE).
