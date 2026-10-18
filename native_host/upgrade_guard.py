"""Fail closed while the Windows installer retires previous download writers."""

from pathlib import Path
import time


def wait_for_upgrade(app_dir: Path) -> None:
    # Intentionally no timeout: a failed/interrupted installer leaves this
    # durable marker behind. Only a successful installer may resume the queue.
    # Diagnostics bypass this gate so rerunning installation can recover.
    while (app_dir / "upgrade-in-progress").exists():
        time.sleep(0.2)
