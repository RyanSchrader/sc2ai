from __future__ import annotations

import subprocess
import sys

from node_runtime import ROOT, discover_frontend_runtime


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else "doctor"
    if action not in {"doctor", "install", "dev", "build", "test"}:
        print(f"Unknown frontend action: {action}", file=sys.stderr)
        return 2

    try:
        runtime = discover_frontend_runtime()
    except RuntimeError as error:
        print(f"Frontend runtime error: {error}", file=sys.stderr)
        return 1

    print(
        f"Using Node {runtime.node_version} at {runtime.node} "
        f"with {runtime.manager} {runtime.manager_version}",
        flush=True,
    )
    if action == "doctor":
        return 0

    try:
        return subprocess.call(
            runtime.command(action),
            cwd=ROOT,
            env=runtime.environment,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
