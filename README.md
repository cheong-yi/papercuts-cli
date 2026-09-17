# Papercuts CLI

Record small, recoverable developer and AI-agent workflow friction without leaving your repository. Papercuts keeps a local, append-only ledger so you can capture an annoyance, review it, and record its resolution.

## Quickstart

Requires Python 3.11+, Git, and Linux on a local filesystem. From a reviewed checkout:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/papercut record --summary 'editor opens slowly'
.venv/bin/papercut list
.venv/bin/papercut resolve <id> --resolution 'disabled the slow extension'
.venv/bin/papercut check
```

Replace `<id>` with the ID returned by `record`. Installation can download build tooling; it is not universally offline. Runtime uses only the Python standard library and Git, with no network or telemetry. To run without installing, use `PYTHONPATH=src python3 -B -m papercuts --help` from the checkout.

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
env -u PYTHONPATH "$INSTALL_ROOT/bin/papercut" --help
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

## Commands

- `record --summary TEXT`: capture friction; optional `--category`, `--expected`, `--observed`, `--evidence-basis`, and `--recurrence-key` add context.
- `resolve ID --resolution TEXT`: resolve a record once.
- `list [--status open|resolved|all] [--limit N]`: show open records by default, with a default limit of 5 (maximum 25), in first-recorded order.
- `render [--status open|resolved|all]`: emit deterministic, inert Markdown to stdout.
- `check`: validate the ledger without changing it.
- `packet --expect-head OID|unborn [--record-id UUID4 ... | --event-id UUID4 ...]`: emit a canonical JSON evidence projection bound to the expected repository HEAD. Selectors are repeatable, unique, and limited to 25; selector types cannot be mixed.

Run `papercut COMMAND --help` for accepted values. Only `record` and `resolve` write ledger events; the other commands are read-only. Rendering and packets are never saved automatically.

## Scope and limitations

Papercuts records observations, not authority. A packet is neither a signature nor an approval, and it does not authorize actions or prove that an observation is true. Capture and resolution are manual. There are no hooks, daemons, automatic transcript collection, integrations, or automatic fixes.

Events live at `<git-common-dir>/papercuts/events.jsonl`. Linked worktrees share one ledger. Storage directories and files are owner-only; readers use shared locks and writers use exclusive locks. Unsafe storage, lock contention, and malformed history fail closed rather than being repaired automatically. Network filesystems and non-Linux platforms are unsupported.

The ledger is bounded to 8 MiB, 10,000 events, and 8,192 bytes per event line. List output is bounded to 4,096 UTF-8 bytes. These limits are intentional; Papercuts is not a general-purpose log collector or issue tracker.

## Privacy

Before writing, Papercuts rejects selected home/drive/UNC paths, URI credentials, private-key headers, specific GitHub/OpenAI-style/AWS/Slack token shapes, and selected Unicode controls. This is a narrow pattern filter, **not general secret detection**. It cannot guarantee that accepted text is safe to share.

Paraphrase sensitive material. Arguments may appear in shell history or process listings; output may appear in terminal scrollback, redirected files, or logs. Review inputs and output destinations yourself. Ledger files are local runtime data, not publication material. The ignore rules are a precaution, not a security boundary.

## Verification

Run the full test suite from the checkout:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

Tests exercise storage safety, privacy boundaries, lifecycle commands, packets, rendering, concurrency, no-network behavior, and packaging. Packaging tests require locally available setuptools and wheel tooling. This repository does not provide CI configuration.

`PUBLIC_FILES.json` is the closed-world source inventory: its sorted `files` array includes the manifest itself and must equal every regular file in the checkout, excluding `.git`. Each other file has a SHA-256 in `sha256`; the manifest omits its own hash to avoid self-reference. Run validation in a clean checkout; generated artifacts belong outside the publication tree.

## Public file map

- `src/papercuts/`: CLI, event model, privacy validation, repository/storage safety, rendering, and packets.
- `tests/` and `tests/fixtures/`: regression tests and synthetic fixtures.
- `pyproject.toml`: package metadata and the `papercut` entry point.
- `MANIFEST.in`: includes the public inventory and `.gitignore` in source distributions; excludes tests and fixtures.
- `.gitignore`: excludes local state and generated files.
- `PUBLIC_FILES.json`: exact publication inventory and content hashes.
- `README.md` and `LICENSE`: public guidance and license.

## Attribution

Conceptual inspiration: [Steve Ruiz's papercuts thread](https://x.com/i/status/2075303919664734295) and [aurorascharff/agent-friction-skill](https://github.com/aurorascharff/agent-friction-skill/commit/945d109afadd5dfbd3dbff46992129c17ec7b098).

Papercuts is an independent implementation. Inspection of this source found no imported source code or runtime dependency from either reference; the attribution is for the concept, not code reuse or endorsement.

## License

MIT © 2026 cheong-yi. See [LICENSE](LICENSE).
