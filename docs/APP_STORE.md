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
profile identity, signs nested Mach-O files and bundles, verifies the sealed app,
creates a signed `.pkg`, and records a package report. It never replaces the
installed application and does not execute the application or its collectors.

Upload with Apple's `altool` using an existing App Store Connect key. Upload
success, `VALID` build processing, submission for review and App Store approval
are distinct states; verify each independently before reporting it complete.

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
