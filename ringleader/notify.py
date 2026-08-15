import contextlib
import subprocess
import sys


def notify(title: str, message: str) -> None:
    if sys.platform != "darwin":
        return
    with contextlib.suppress(OSError):
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False,
        )
