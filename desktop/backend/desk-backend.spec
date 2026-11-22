# PyInstaller spec for the desk backend bundled inside the desktop app.
# Build from the repo root:  pyinstaller desktop/backend/desk-backend.spec --noconfirm
# Output: dist/desk-backend/ (a folder: starts in about a second, unlike a one-file build)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hidden = []
for pkg in ("desk", "tradingagents", "cli", "uvicorn", "langchain_core", "langchain_openai",
            "langchain_anthropic", "langchain_google_genai", "langgraph", "mcp", "webauthn"):
    hidden += collect_submodules(pkg, filter=lambda name: not {"tests", "dev"} & set(name.split(".")))

datas = collect_data_files("desk") + collect_data_files("cli") + collect_data_files("tradingagents")
for dist in ("tradingagents", "langchain_core", "langgraph", "mcp", "yfinance", "fastapi", "uvicorn"):
    try:
        datas += copy_metadata(dist)
    except Exception:  # noqa: BLE001 - metadata is optional for most of these
        pass

a = Analysis(
    ["entry.py"],
    pathex=["../.."],
    hiddenimports=hidden,
    datas=datas,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "webview", "PyQt5", "PyQt6", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="desk-backend",
    console=True,            # the app starts it hidden; a console build keeps stdin/stdout pipes working on Windows
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="desk-backend", upx=False)
