# Desktop app

A native window (Tauri) with the desk backend frozen by PyInstaller inside it. Installers for macOS (Apple Silicon and Intel), Windows and Linux are built by [`.github/workflows/desktop.yml`](../.github/workflows/desktop.yml) and published as GitHub releases tagged `desktop-v<version>`.

## How it works

- **On this computer (default)**: the app starts `tradesetu-backend --port <free port>` (that is `tradesetu --desktop`) on 127.0.0.1 and shows it. The backend quits when the app closes its stdin, so it never outlives the window, even after a crash. Data lives in `~/.tradingagents/desk`, shared with a pip install.
- **My server**: Desk menu > Connection settings stores a URL in the app's config folder (`desktop.json`); the window then opens that hosted desk.
- Links to other sites and the Kite sign-in open in the system browser. Downloads (Excel exports) go to the Downloads folder.
- Backend log: Desk menu > Show backend log.

## Build locally

```bash
pip install ".[desk]" pyinstaller
pyinstaller desktop/backend/tradesetu-backend.spec --noconfirm --distpath dist
python desktop/backend/smoke.py dist/tradesetu-backend/tradesetu-backend
cd desktop/app && npm ci && npx tauri build
```

Needs Rust and Node.js 20+; on Linux also `libwebkit2gtk-4.1-dev libappindicator3-dev librsvg2-dev patchelf`.

## Release

Bump `version` in `desktop/app/src-tauri/tauri.conf.json` and `desktop/app/package.json`, then merge to main: the workflow publishes release `desktop-v<version>` with the installers. Pull requests that touch the app build the installers as workflow artifacts only.

## Signing

Builds are unsigned: macOS Gatekeeper and Windows SmartScreen warn on first start. To sign, add an Apple Developer ID certificate (`APPLE_CERTIFICATE`, `APPLE_CERTIFICATE_PASSWORD`, `APPLE_SIGNING_IDENTITY`, `APPLE_ID`, `APPLE_PASSWORD`, `APPLE_TEAM_ID`) and a Windows code-signing certificate as repository secrets and pass them as environment variables to the "Build the app and installers" step (Tauri reads the Apple ones from there; drop `signingIdentity: "-"` from tauri.conf.json then).
