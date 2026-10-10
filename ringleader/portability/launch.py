from __future__ import annotations

import os
import shutil
import subprocess
from typing import Protocol

from .capabilities import RunRequest, RunResult
from .model import PreservationError


class ProbeRunner:
    def run(self, request: RunRequest) -> RunResult:
        environment = {"PATH": os.environ.get("PATH", os.defpath), **dict(request.env)}
        result = subprocess.run(
            request.argv,
            cwd=request.cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return RunResult(result.returncode, result.stdout, result.stderr)


class Launcher(Protocol):
    def validate(self, request: RunRequest) -> None: ...
    def launch(self, request: RunRequest) -> None: ...


class ExecLauncher:
    def validate(self, request: RunRequest) -> None:
        executable = shutil.which(request.argv[0], path=os.environ.get("PATH", os.defpath))
        if executable is None:
            raise PreservationError(
                f"Native executable not found or not executable: {request.argv[0]}"
            )
        if not request.cwd.is_dir() or not os.access(request.cwd, os.R_OK | os.X_OK):
            raise PreservationError(
                f"Native launch workspace is missing or unusable: {request.cwd}"
            )

    def launch(self, request: RunRequest) -> None:
        self.validate(request)
        previous = os.getcwd()
        try:
            os.chdir(request.cwd)
            os.execvpe(request.argv[0], request.argv, {**os.environ, **dict(request.env)})
        finally:
            os.chdir(previous)
