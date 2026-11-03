# Mac App Store packaging

The App Store Connect application is **Monica Desktop**, Apple ID `6829808522`,
bundle identifier `org.monica.desktop`. The first version is `0.1.0`.

App Store packaging is separate from the existing ad-hoc GitHub release workflow.
It requires an Apple Distribution identity, Mac Installer Distribution identity
and a `MAC_APP_STORE` provisioning profile for the app. Keep their private keys
and profiles outside the repository. Do not run the generic skill's certificate
creation script when the existing team identities are available.

Build the public runtime without face weights, then the native application:

```sh
python3 apps/desktop/scripts/bundle_runtime.py --external-face-models \
  --chatlog-binary /path/to/public-chatlog-build
npm run build:desktop
```

Package a separately copied application with inherited sandbox signatures on its
native helpers, then sign the installer:

```sh
python3 apps/desktop/scripts/package_appstore.py \
  --app apps/desktop/src-tauri/target/release/bundle/macos/Monica.app \
  --profile /private/path/MonicaDesktop.provisionprofile \
  --identity 'Apple Distribution: Your Name (TEAMID)' \
  --installer-identity '3rd Party Mac Developer Installer: Your Name (TEAMID)' \
  --team-id TEAMID --build 1 --output .local-validation/store-build-1
```

Use a new output directory and increasing build number. The script verifies
profile identity, restores versioned framework links omitted by Tauri's resource
copier, signs nested Mach-O files and bundles, verifies the sealed app,
creates a signed `.pkg`, and records a package report. It never replaces the
installed application and does not execute the application or its collectors.

Upload with Apple's `altool` using an existing App Store Connect key. Upload
success, `VALID` build processing, submission for review and App Store approval
are distinct states; verify each independently before reporting it complete.

Xcode 26.5's default uploader stalled while generating the asset description on
this build Mac. The built-in compatibility uploader completed the upload:

```sh
xcrun altool --use-old-altool --upload-app -t macos \
  -f .local-validation/store-build-1/Monica-0.1.0-1-macos.pkg \
  --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"
```

The upload receipt's delivery UUID can be queried through the authenticated
`GET /v1/buildUploads/{deliveryUUID}` API while the binary is still processing.
At that stage `/v1/builds` may not yet contain a selectable build. Store receipts
outside source control; don't print authentication tokens into build logs.

The initial upload failed Apple's structural validation with `90291`, `90292`
and related `90238` signature errors. The Chromium framework's `Versions/Current`
and top-level links had been omitted or flattened in the Tauri application
resources. Packaging now restores the canonical versioned layout before signing.
The repair requires one unambiguous version and validates its `FMWK` metadata;
it does not remove the browser or any collector. Use a new build number when
uploading the corrected package.

## Sandbox status

The App Store package enables App Sandbox, network client/server access and
user-selected file access. Native helper executables have the two sandbox
inheritance entitlements. Foundation resolves the container home; initial
PostgreSQL setup receives the bundled share directory explicitly.

Signing verification is not a sandbox runtime functional test. The first
submission was prepared at the owner's request without completing that test.
Persistent security-scoped source access, PostgreSQL IPC, bundled browser
automation and login-item behavior still require runtime validation before
claiming App Store feature parity. Do not substitute the ordinary GitHub smoke
report for that validation.

Store screenshots use synthetic local data from the public build. The privacy
policy is [PRIVACY.md](PRIVACY.md). Source sessions, private databases, model
weights and developer credentials must not be included in the upload.

## Reduced Store edition (build 3 onward)

App Review rejected build 2 under 2.4.5 and 2.5.1. The loopback server entitlement
is required by the actual architecture: Python control/API services, PostgreSQL,
Next.js and Caddy listen exclusively on 127.0.0.1. Explain this in the review notes
and Resolution Center; removing the entitlement would break local operation.

The owner approved a reduced Store edition. It retains the local CRM, WeChat,
archive ingestion, CDC/history and optional user-configured model APIs. Browser
collectors and face processing remain in the standalone edition. Store settings
offer explicit folder import for Monica's published Instagram profiles and
LinkedIn JSON/assets snapshots. These are Monica archive formats, not arbitrary
Instagram/LinkedIn account-export ZIPs. Snapshot publication stays atomic and
uses the existing independent import/projection lanes.

`package_appstore.py --lite` computes the backend's installed Python dependency
closure and removes the browser, Playwright, scientific/face dependencies, unused
Tk/Perl modules, source GeoNames JSON, and Node development tools. The generated
city SQLite index is validated before removing its build-time inputs. Uvicorn
uses its asyncio/h11 implementation instead of uvloop's additional libuv copy.
Standalone runtime assembly and collector source pins are unchanged.

Build the Store Node from the checksum-verified official Node 22.21.1 source:

```sh
uv run --python 3.12 python apps/desktop/scripts/build_appstore_node.py \
  --output .local-validation/store-node/node --jobs 6
```

The build replaces libuv's macOS process-title function at source level with its
public thread-name implementation. This removes the private LaunchServices
lookups, rather than concealing their symbols in a binary. Inspector, npm and
Corepack are not part of this runtime. Its source/binary hashes are recorded in
`node.json` next to the output. Pass `--lite --node-binary <that output>` to the
existing packaging command, using a fresh build number/output directory and a
built app containing the updated settings shell and desktop Python sources.
The packager checks that report and runs `verify_appstore_apis.py` against the
actual Mach-O inputs before signing. The scan checks the 150 APIs named by Apple
in this rejection; Apple's own analysis may identify additional issues.

Run backend-module, immutable city-index, media conversion and archive snapshot
checks against the reduced resource tree before signing. Use the matching build
Python interpreter for those import checks: the distribution-signed interpreter
has sandbox inheritance entitlements and cannot be launched as a standalone
terminal executable. Those component checks do not establish full sandbox
runtime compatibility. The owner's request to omit that full test still applies.
