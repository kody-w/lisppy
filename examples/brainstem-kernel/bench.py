#!/usr/bin/env python3
"""Compare all oracle scenarios: 31 interleaved warm samples, no third-party imports.

Uses the oracle's cmd: environment and grail: runner with the same fake model and
fixtures. GRAIL_DIR and GRAIL_PYTHON retain their existing oracle meanings. Boot
includes the first successful /health; RSS is resident process memory after the
scenario run. Contract mismatches fail; latency fails only when both >25% and >2 ms slower.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ORACLE_DIR = HERE.parent / "rapp-1" / "conformance" / "grail"
SAMPLES = 31


def load_oracle():
    spec = importlib.util.spec_from_file_location("host_perf_oracle", ORACLE_DIR / "run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def wait_ready(process, url, log, timeout=40):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited ({process.returncode}):\n{log.read_text()}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"startup health failed ({error.code}): {error.read()!r}") from error
        except OSError:
            time.sleep(.005)
    raise RuntimeError(f"server did not become ready: {url}\n{log.read_text()}")


def launch(stack, oracle, command, environment, cwd, log, url):
    handle = stack.enter_context(log.open("w"))
    started = time.perf_counter()
    process = subprocess.Popen(command, env=environment, cwd=cwd,
                               stdout=handle, stderr=subprocess.STDOUT)
    stack.callback(oracle.stop_process, process)
    wait_ready(process, url, log)
    return process, (time.perf_counter() - started) * 1000


def rss_mb(process):
    result = subprocess.run(["ps", "-o", "rss=", "-p", str(process.pid)],
                            check=True, capture_output=True, text=True)
    return int(result.stdout.strip()) / 1024


def timed_call(oracle, url, scenario):
    start = time.perf_counter()
    response = oracle.call_http(url, scenario)
    return (time.perf_counter() - start) * 1000, response


def compare_scenarios(oracle, lispy_url, grail_url, samples=SAMPLES):
    rows = []
    for scenario in oracle.SCENARIOS:
        timings = [[], []]
        urls = (lispy_url, grail_url)
        # Warm auth, imports and connections equally; never include startup in request medians.
        for url in urls:
            oracle.call_http(url, scenario)
        for repetition in range(samples):
            results = {}
            for index in ((0, 1) if repetition % 2 == 0 else (1, 0)):
                duration, response = timed_call(oracle, urls[index], scenario)
                timings[index].append(duration)
                results[index] = oracle.contract_view(
                    scenario, *response, compare_error_text=True, compare_health_agents=True)
            if results[0] != results[1]:
                raise RuntimeError(f"{scenario.name}: contract mismatch: {results}")
        rows.append((scenario.name, statistics.median(timings[0]), statistics.median(timings[1])))
    return rows


def health_median(oracle, url):
    scenario = next(s for s in oracle.SCENARIOS if s.path == "/health")
    durations = []
    for _ in range(30):
        duration, (code, body) = timed_call(oracle, url, scenario)
        if code != 200 or body.get("status") != "ok":
            raise RuntimeError(f"health failed: {code} {body}")
        durations.append(duration)
    return statistics.median(durations)


def print_results(rows, metrics):
    print("31 interleaved warm samples per scenario; medians in ms")
    print(f"{'scenario':32} {'lispy ms':>10} {'grail ms':>10} {'ratio':>8}")
    failed = []
    for name, lispy, grail in rows:
        ratio = lispy / grail
        print(f"{name:32} {lispy:10.3f} {grail:10.3f} {ratio:8.3f}")
        if lispy > grail * 1.25 and lispy - grail > 2:
            failed.append(name)
    print(f"\n{'metric':32} {'lispy':>10} {'grail':>10}")
    for name, lispy, grail in metrics:
        print(f"{name:32} {lispy:10.3f} {grail:10.3f}")
    print(f"\n{len(rows)} scenarios identical")
    if failed:
        print("FAIL (>25% AND >2 ms slower): " + ", ".join(failed))
    else:
        print("PASS: no scenario both >25% and >2 ms slower")
    return 1 if failed else 0


def main():
    oracle = load_oracle()
    grail, blob = oracle.verify_oracle(
        os.environ.get("GRAIL_DIR", str(HERE.parent / "grail-pin-0616" / "rapp_brainstem")),
        oracle.load_pin())
    python = os.environ.get("GRAIL_PYTHON", str(
        oracle.DEFAULT_GRAIL_PYTHON if oracle.DEFAULT_GRAIL_PYTHON.exists() else Path(sys.executable)))
    with contextlib.ExitStack() as stack:
        work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="lispy-host-perf-")))
        fake_port = oracle.free_port()
        fake_url = f"http://127.0.0.1:{fake_port}"
        launch(stack, oracle, [sys.executable, str(ORACLE_DIR / "fake_model.py"), str(fake_port)],
               dict(os.environ), work, work / "fake.log", fake_url + "/models")
        grail_work = oracle.prepare_grail(grail, work / "grail")
        grail_port = oracle.free_port()
        grail_url = f"http://127.0.0.1:{grail_port}"
        grail_process, grail_boot = launch(
            stack, oracle,
            [python, str(ORACLE_DIR / "run_grail.py"), str(grail_work), str(grail_port), str(fake_port)],
            oracle.grail_environment(work / "grail-home", expected_blob=blob),
            work, work / "grail.log", grail_url + "/health")
        candidate = work / "cmd"
        candidate.mkdir()
        agents = candidate / "agents"
        shutil.copytree(ORACLE_DIR / "agents", agents, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(grail / "agents" / "basic_agent.py", agents / "basic_agent.py")
        home = candidate / "home"
        home.mkdir()
        lispy_port = oracle.free_port()
        lispy_url = f"http://127.0.0.1:{lispy_port}"
        environment = dict(
            os.environ, HOME=str(home), PORT=str(lispy_port), GITHUB_TOKEN="ghu_fake",
            GITHUB_MODEL=oracle.MODEL, AGENTS_PATH=str(agents), VOICE_MODE="false",
            FAKE_MODEL_URL=fake_url + "/v1", FAKE_TOKEN_URL=fake_url + "/token",
            COPILOT_TOKEN_URL=fake_url + "/token")
        lispy_process, lispy_boot = launch(
            stack, oracle, [sys.executable, str(HERE / "lispy_host.py")],
            environment, candidate, work / "lispy.log", lispy_url + "/health")
        rows = compare_scenarios(oracle, lispy_url, grail_url)
        metrics = [
            ("boot-to-serve ms", lispy_boot, grail_boot),
            ("RSS MB", rss_mb(lispy_process), rss_mb(grail_process)),
            ("/health median ms (30)", health_median(oracle, lispy_url), health_median(oracle, grail_url)),
        ]
        return print_results(rows, metrics)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"benchmark failed: {error}", file=sys.stderr)
        sys.exit(1)
