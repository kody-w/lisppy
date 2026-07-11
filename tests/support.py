import functools
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = (sys.executable, str(ROOT / "lisp.py"))
DEFAULT_TIMEOUT = 20


def _environment(overrides):
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    for name, value in (overrides or {}).items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = str(value)
    return environment


def run_cli(
    *arguments,
    stdin=None,
    timeout=DEFAULT_TIMEOUT,
    env_overrides=None,
    cwd=ROOT,
):
    return subprocess.run(
        [*CLI, *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_environment(env_overrides),
        timeout=timeout,
        check=False,
    )


def run_module(
    *arguments,
    stdin=None,
    timeout=DEFAULT_TIMEOUT,
    env_overrides=None,
    cwd=ROOT,
):
    return subprocess.run(
        [sys.executable, "-m", "lisppy", *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_environment(env_overrides),
        timeout=timeout,
        check=False,
    )


def run_python(
    *arguments,
    stdin=None,
    timeout=DEFAULT_TIMEOUT,
    env_overrides=None,
    cwd=ROOT,
):
    return subprocess.run(
        [sys.executable, *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_environment(env_overrides),
        timeout=timeout,
        check=False,
    )


@functools.lru_cache(maxsize=1)
def replay_bundle_bytes():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "doctor-replay.json"
        result = run_cli(
            "--doctor",
            "--json",
            "--export-replay",
            str(path),
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout)
        return path.read_bytes()


@contextmanager
def fresh_replay_bundle():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "doctor-replay.json"
        path.write_bytes(replay_bundle_bytes())
        yield path
