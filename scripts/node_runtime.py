from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MINIMUM_NODE_MAJOR = 24
ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


@dataclass(frozen=True)
class FrontendRuntime:
    node: Path
    node_version: str
    manager: str
    manager_version: str
    launcher: tuple[str, ...]

    @property
    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(self.node.parent), environment.get("PATH", "")]
        )
        if self.manager == "pnpm":
            # The Codex-bundled pnpm performs a dependency status check before
            # scripts and must not prompt when launched through make.
            environment["CI"] = "true"
        return environment

    def command(self, action: str) -> list[str]:
        if action == "install":
            if self.manager == "pnpm":
                return [
                    *self.launcher,
                    "--dir",
                    str(FRONTEND),
                    "install",
                    "--lockfile=false",
                ]
            return [*self.launcher, "ci", "--prefix", str(FRONTEND)]

        if self.manager == "npm":
            return [*self.launcher, "run", action, "--prefix", str(FRONTEND)]
        return [
            *self.launcher,
            "--dir",
            str(FRONTEND),
            "run",
            action,
        ]


def _output(command: list[str], environment: dict[str, str] | None = None) -> str | None:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def _major(version: str | None) -> int | None:
    if not version:
        return None
    try:
        return int(version.removeprefix("v").split(".", 1)[0])
    except ValueError:
        return None


def _candidate_nodes() -> list[Path]:
    explicit = os.environ.get("SC2_STUDIO_NODE")
    path_node = shutil.which("node")
    candidates = [
        Path(explicit).expanduser() if explicit else None,
        Path(path_node) if path_node else None,
        Path("/opt/homebrew/bin/node"),
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node",
        Path("/usr/local/bin/node"),
    ]
    unique: list[Path] = []
    for candidate in candidates:
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique


def _pnpm_cli_for(node: Path) -> Path | None:
    candidate = node.parent.parent / "node_modules/pnpm/bin/pnpm.mjs"
    return candidate if candidate.is_file() else None


def discover_frontend_runtime() -> FrontendRuntime:
    observations: list[str] = []
    explicit = os.environ.get("SC2_STUDIO_NODE")

    for node in _candidate_nodes():
        if not node.is_file():
            if explicit and node == Path(explicit).expanduser():
                raise RuntimeError(f"SC2_STUDIO_NODE does not exist: {node}")
            continue

        node_version = _output([str(node), "--version"])
        node_major = _major(node_version)
        observations.append(f"{node} ({node_version or 'unusable'})")
        if node_major is None or node_major < MINIMUM_NODE_MAJOR:
            if explicit and node == Path(explicit).expanduser():
                raise RuntimeError(
                    f"SC2_STUDIO_NODE is {node_version}; Node.js "
                    f"{MINIMUM_NODE_MAJOR}+ is required."
                )
            continue

        pnpm_cli = _pnpm_cli_for(node)
        if pnpm_cli:
            pnpm_version = _output([str(node), str(pnpm_cli), "--version"])
            if pnpm_version:
                return FrontendRuntime(
                    node=node,
                    node_version=node_version or "unknown",
                    manager="pnpm",
                    manager_version=pnpm_version,
                    launcher=(str(node), str(pnpm_cli)),
                )

        environment = os.environ.copy()
        environment["PATH"] = os.pathsep.join(
            [str(node.parent), environment.get("PATH", "")]
        )
        npm = shutil.which("npm", path=environment["PATH"])
        npm_version = _output([npm, "--version"], environment) if npm else None
        if npm and (_major(npm_version) or 0) >= 10:
            return FrontendRuntime(
                node=node,
                node_version=node_version or "unknown",
                manager="npm",
                manager_version=npm_version or "unknown",
                launcher=(npm,),
            )

    found = ", ".join(observations) if observations else "none"
    raise RuntimeError(
        "No supported frontend runtime was found. "
        f"SC2 Bot Studio requires Node.js {MINIMUM_NODE_MAJOR}+ with npm 10+ "
        "or the Node runtime bundled with Codex. "
        f"Detected Node candidates: {found}. "
        "Install Node.js 24 LTS, or set SC2_STUDIO_NODE to its node executable."
    )
