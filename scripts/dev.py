from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from node_runtime import discover_frontend_runtime

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    environment = os.environ.copy()
    try:
        frontend_runtime = discover_frontend_runtime()
    except RuntimeError as error:
        print(f"Frontend runtime error: {error}", file=sys.stderr)
        return 1

    print(
        f"Using Node {frontend_runtime.node_version} with "
        f"{frontend_runtime.manager} {frontend_runtime.manager_version}",
        flush=True,
    )
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "studio.app:app", "--host", "127.0.0.1", "--port", "8000", "--reload"],
        cwd=ROOT,
        env=environment,
    )
    frontend = subprocess.Popen(
        frontend_runtime.command("dev"),
        cwd=ROOT,
        env=frontend_runtime.environment,
    )
    processes = [backend, frontend]

    def stop(_signum=None, _frame=None):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    return next((process.returncode or 0 for process in processes if process.returncode), 0)


if __name__ == "__main__":
    raise SystemExit(main())
