# Desktop CI/CD

The pipeline is [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

| Trigger | Validation and build | Publication |
| --- | --- | --- |
| Pull request | All checks and macOS app build | Actions artifact only |
| Push to `main` | All checks and macOS app build | Immutable experimental `preview-<12-character SHA>` release |
| Push `v<version>` tag | All checks and macOS app build | Versioned experimental release; tag must match app version |
| Run workflow manually | All checks and build | Controlled by the `publish` checkbox; only main or exact version tags may publish |

Current releases remain prereleases. The workflow does not deploy a NAS, publish production container images, submit to the App Store, or install an update on users' machines.

## Verification

- Locked Python API dependencies and Ruff, then the full PostgreSQL integration suite in UTC and America/Chicago. The isolated PostgreSQL service enables logical WAL for CDC tests.
- Runtime, bundle contracts and collector fixtures; per-test timeouts expose hangs. Collector tests use their own locked environment, including Playwright.
- Web and settings TypeScript checks, Next.js production build, and Chatlog media fixtures.
- A clean `macos-26` arm64 runner builds the public patched Chatlog source, Python 3.12.11, Node, PostgreSQL 16, Caddy, Chromium and all source collectors. Rust is pinned to 1.92.0 and Go to 1.25.5.
- Native verification checks all vendored digests, installed dependency source files, pinned dependencies, 12 entrypoints and real Chromium rendering. The unavailable `linkedin-cli` repository is replaced by the exact MIT runtime source snapshot, with a fixed package version and checked provenance.
- The assembled app runs in a separate empty data directory. Account bootstrap, signed source imports, independent projection, asynchronous WAL history, actual Web before/after values, pause/resume, database failure recovery and complete shutdown must pass.
- Deep strict codesign verification runs before and after runtime tests.

The source checks and macOS build can run concurrently. Publication requires every job to succeed. No manually configured secrets are needed for the current experimental pipeline. Only the publish job has `contents: write`; build jobs retain read-only permissions and do not persist checkout credentials.

## Download files

Each release contains:

- A ZIP preserving `Monica.app` and its native file structure.
- `SHA256SUMS` and `release.json`, recording the source commit, workflow run, version, platform, signing state and asset sizes/hashes.
- Two small public verification reports.

Every asset is checked against [GitHub's 2 GiB per-file limit](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas). Only the checked public files are uploaded. Runtime settings, cookies, test databases, logs and model weights are excluded. Uploads remain a draft until the full asset set is present. Retries never replace an already published binary, and a newer main commit prevents an older build from publishing a stale preview.

Download from [Releases](https://github.com/hmumixaM/monica-desktop/releases), extract `Monica.app` and move it to Applications. These are experimental **ad-hoc signed, unnotarized** builds for **Apple Silicon / macOS 26+**. macOS may require approval in System Settings → Privacy & Security. Existing application data stays outside the app bundle.

## Face models

Public builds use `--external-face-models`. Face analysis code remains included; weights are supplied locally by the user. The [upstream model terms](https://github.com/deepinsight/insightface#license) are separate from the source-code license.

Put `det_10g.onnx` and `w600k_r50.onnx` in:

```text
~/Library/Application Support/Monica/models/insightface/models/buffalo_l/
```

Enable face analysis and resume tasks. Missing weights show a specific face-source status and do not stop imports. Personal model-inclusive builds remain supported with `--face-models`.

## Version tags

Update the application version in `apps/desktop/src-tauri/tauri.conf.json` and `Cargo.toml`/`Cargo.lock`, merge to main, then push its matching version tag. For example, app version `0.1.0` uses `v0.1.0`. Tags cannot publish a different application version.

Developer ID signing, notarization and an app updater can be configured separately when those services are available. They are not prerequisites for the current experimental releases.
