# Desktop and Android app

A native window (Tauri) with the desk backend frozen by PyInstaller inside it. Installers for macOS (Apple Silicon and Intel), Windows and Linux are built by [`.github/workflows/desktop.yml`](../.github/workflows/desktop.yml) and published as GitHub releases tagged `desktop-v<version>`.

## How it works

- **On this computer (default)**: the app starts `desk-backend --port <free port>` (the desk run with `--desktop`) on 127.0.0.1 and shows it. The backend quits when the app closes its stdin, so it never outlives the window, even after a crash. Data lives in `~/.tradingagents/desk`, shared with a pip install.
- **My server**: Desk menu > Connection settings stores a URL in the app's config folder (`desktop.json`); the window then opens that hosted desk.
- Kite: save your own Kite Connect API key and secret in Settings → Broker and set the app's redirect URL to `http://127.0.0.1:47800/broker/callback` (the local desk uses port 47800 when it is free). No Node.js or Kite MCP server is needed, and one login lasts until 6 AM. API orders still need the static IP registered on your Kite app.
- Links to other sites and the Kite sign-in open in the system browser. Downloads (Excel exports) go to the Downloads folder.
- Backend log: Desk menu > Show backend log.

## Android

The same Tauri app builds an APK (job `android` in the workflow). A phone cannot run the Python desk, so the Android app has server mode only: on first start it asks for the https address of your hosted desk and opens it full screen. Links to other sites and the Kite login go to the phone's browser. The website and its PWA are unchanged.

The APK is signed with the key in repository secrets `ANDROID_KEYSTORE_BASE64` (base64 of a .jks), `ANDROID_KEYSTORE_PASSWORD` and `ANDROID_KEY_ALIAS`. Without them each build is signed with a one-off key, so installing a newer version means uninstalling the old one first (only the server address is lost). Create a key once with:

```bash
keytool -genkeypair -keystore release.jks -alias upload -keyalg RSA -keysize 3072 -validity 10000
base64 -w0 release.jks      # paste into ANDROID_KEYSTORE_BASE64; keep release.jks safe, it cannot be replaced
```

## Build locally

```bash
pip install ".[desk]" pyinstaller
pyinstaller desktop/backend/desk-backend.spec --noconfirm --distpath dist
python desktop/backend/smoke.py dist/desk-backend/desk-backend
cd desktop/app && npm ci && npx tauri build
```

Needs Rust and Node.js 20+; on Linux also `libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf`.

## Release

Merging to main never publishes a release. Pull requests that touch the app build the installers as workflow artifacts only, so you can merge many changes first and release when you choose:

1. On GitHub open **Actions > Desktop app > Run workflow**.
2. Keep branch **main**, type the new version (for example `0.8.0`, higher than the last `desktop-v…` release), and press **Run workflow**.
3. In about 15 minutes release `desktop-v<version>` appears under Releases with the macOS, Windows, Linux and Android installers.

Pushing a tag `desktop-v<version>` does the same. The version you type is stamped into the build; `tauri.conf.json` and `package.json` in the repository do not need a bump. A version that is already released is refused. Run workflow on any branch other than main builds the installers without publishing.

## Signing

Builds are unsigned: macOS Gatekeeper and Windows SmartScreen warn on first start. To sign, add an Apple Developer ID certificate (`APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`) and a Windows code-signing certificate as repository secrets and pass them as environment variables to the "Build the app and installers" step (Tauri reads the Apple ones from there; drop `signingIdentity: "-"` from tauri.conf.json then).
