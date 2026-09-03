from __future__ import annotations

from pathlib import Path
import socket
import subprocess
import sys
import time
import webbrowser


def find_free_port(start=8501, stop=8520):
    for port in range(start, stop + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free localhost port found from {start} to {stop}.")


def main():
    project_root = Path(__file__).resolve().parents[1]
    app_path = project_root / "drone_optimizer" / "app.py"
    port = find_free_port()
    url = f"http://localhost:{port}"

    print("=" * 72)
    print("Drone Optimizer")
    print("=" * 72)
    print(f"Opening {url}")
    print("Keep this window open while using the optimizer.")
    print("Press Ctrl+C here when you want to stop the local app.")
    print()

    # Give Streamlit a moment to start before opening the browser.
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.headless", "true",
        "--server.address", "127.0.0.1",
        "--server.port", str(port),
        "--browser.gatherUsageStats", "false",
    ]
    proc = subprocess.Popen(cmd, cwd=str(project_root))
    time.sleep(1.8)
    webbrowser.open(url)

    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
