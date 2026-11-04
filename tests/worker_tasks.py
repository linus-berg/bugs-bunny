"""Module-level functions for pool tests; worker processes import them by reference."""

import os
import time

from app.errors import ServiceError


def init(tag="ready"):
    return {"tag": tag, "pid": os.getpid()}


def echo(value):
    return value


def pid():
    return os.getpid()


def sleep(seconds):
    time.sleep(seconds)
    return os.getpid()


def crash():
    os._exit(13)


def fail():
    raise RuntimeError("boom")


def reject():
    raise ServiceError(422, "rejected", "nope", headers={"X-Test": "1"})


def unpicklable():
    return lambda: None


def failing_init():
    raise RuntimeError("cannot start")


def spawn_child_then_hang(pid_file):
    """Start a long-running child the way office.LibreOffice does, then hang until killed."""
    import subprocess

    from app.office import LibreOffice

    prefix = LibreOffice("unused", timeout=1)._prefix
    child = subprocess.Popen([*prefix, "sleep", "300"])
    with open(pid_file, "w") as f:
        f.write(str(child.pid))
    time.sleep(300)
