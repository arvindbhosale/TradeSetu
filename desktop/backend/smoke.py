"""Start the frozen backend, check it serves the desk, then check it quits when its stdin closes.

    python desktop/backend/smoke.py dist/tradesetu-backend/tradesetu-backend[.exe]
"""

import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(exe: str) -> None:
    port = free_port()
    home = tempfile.mkdtemp()
    env = {**__import__("os").environ, "TRADESETU_HOME": home, "BHARAT_DESK_HOME": home}
    proc = subprocess.Popen([exe, "--port", str(port)], stdin=subprocess.PIPE, env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 120
        while True:
            if proc.poll() is not None:
                sys.exit(f"backend exited early with {proc.returncode}")
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.time() > deadline:
                sys.exit("backend did not answer /healthz within 120s")
            time.sleep(0.5)
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            page = r.read().decode("utf-8", "replace")
        assert "<html" in page.lower(), "the desk page did not load"
        for asset in ("/static/app.js", "/static/app.css", "/static/lightweight-charts.js"):
            with urllib.request.urlopen(base + asset, timeout=10) as r:
                assert r.status == 200 and r.read(), f"{asset} missing"
        print(f"backend serves the desk on port {port} after {120 - (deadline - time.time()):.1f}s")
        proc.stdin.close()            # the desktop app closing
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            sys.exit("backend did not quit within 30s of its stdin closing")
        print("backend quit when stdin closed: OK")
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    main(sys.argv[1])
