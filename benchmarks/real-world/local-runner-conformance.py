#!/usr/bin/env python3
"""Run checked-in workflow fixtures through the current runner and server.

This is deliberately local: it never dispatches to GitHub.  The workflow and
job outcome records are written in the same shape as batch-conformance.sh so
runner-conformance.py can compare them with the committed official oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def wait_for_health(url: str, process: subprocess.Popen[str], log: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"server exited with {process.returncode}; see {log}")
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=2) as response:
                if 200 <= response.status < 300:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"server did not become ready; see {log}")


def command_output(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def submit(
    client: Path,
    server_url: str,
    token: str,
    workflow: Path,
    workspace: Path,
) -> str:
    env = os.environ.copy()
    env["PRELOOP_SYSTEM_TOKEN"] = token
    output = command_output(
        [
            str(client),
            "--server",
            server_url,
            "submit",
            "-W",
            str(workflow),
            "--workspace-root",
            str(workspace),
            "--event",
            "workflow_dispatch",
            "--repository",
            "local/runner-conformance",
            "--git-ref",
            "refs/heads/main",
        ],
        env=env,
    )
    try:
        accepted = json.loads(output)
        return str(accepted["run_id"])
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(f"client returned invalid submission JSON: {output!r}") from error


def configure_runner(runner: Path, root: Path, server_url: str, token: str) -> None:
    env = os.environ.copy()
    env["PRELOOP_SYSTEM_TOKEN"] = token
    command_output(
        [
            str(runner),
            "--runner-root",
            str(root),
            "configure",
            "--url",
            server_url,
            "--token",
            "t",
            "--name",
            "runner-light-local",
            "--unattended",
            "--replace",
            "--no-externals",
            "--labels",
            "self-hosted,Linux,X64",
        ],
        env=env,
    )


def step_record(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(step.get("name") or ""),
        "conclusion": str(step.get("conclusion") or ""),
        "number": step.get("runner_number") or 0,
    }


def semantic_result(run: dict[str, Any]) -> dict[str, Any]:
    jobs = []
    for job in run.get("jobs_list", []):
        if not isinstance(job, dict):
            continue
        jobs.append(
            {
                "name": str(job.get("name") or ""),
                "conclusion": str(job.get("conclusion") or ""),
                "steps": [
                    step_record(step)
                    for step in job.get("steps", [])
                    if isinstance(step, dict)
                ],
            }
        )
    conclusion = str(run.get("conclusion") or run.get("status") or "unknown")
    return {"conclusion": conclusion, "jobs": jobs}


def scenario_workflow(scenario: Path) -> tuple[Path, int]:
    manifest = scenario / "scenario.toml"
    import tomllib

    metadata = tomllib.loads(manifest.read_text())
    steps = metadata.get("steps", [])
    submit_steps = [step for step in steps if step.get("kind") == "submit_workflow"]
    if len(submit_steps) != 1:
        raise RuntimeError(f"{manifest}: expected exactly one submit_workflow step")
    workflow = scenario / str(submit_steps[0]["path"])
    if not workflow.is_file():
        raise RuntimeError(f"{manifest}: workflow does not exist: {workflow}")
    return workflow, int(metadata.get("duration_seconds_max", 300))


def wait_for_run(
    server_url: str,
    token: str,
    run_id: str,
    runner: subprocess.Popen[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = http_json(f"{server_url}/api/v1/runs/{run_id}", token)
        if last.get("conclusion") is not None:
            return last
        if runner.poll() is not None:
            raise RuntimeError(
                f"runner exited with {runner.returncode} before run {run_id} completed"
            )
        time.sleep(1)
    raise RuntimeError(
        f"run {run_id} did not complete within {timeout_seconds}s; last={last}"
    )


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def official_workflows(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        workflow = record.get("workflow")
        if workflow:
            names.add(str(workflow))
    if not names:
        raise RuntimeError(f"official oracle has no workflow records: {path}")
    return names


def scenario_names(root: Path, official: Path) -> list[str]:
    import tomllib

    names = official_workflows(official)
    # Include every checked-in scenario that has exactly one runnable
    # submit_workflow step. Other manifests are setup-only fixtures and cannot
    # be submitted by this harness.
    for manifest in root.glob("*/scenario.toml"):
        metadata = tomllib.loads(manifest.read_text())
        steps = metadata.get("steps", [])
        submit_steps = [
            step
            for step in steps
            if isinstance(step, dict) and step.get("kind") == "submit_workflow"
        ]
        if len(submit_steps) == 1:
            names.add(manifest.parent.name)
    return sorted(names)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-binary", type=Path, required=True)
    parser.add_argument("--runner-binary", type=Path, required=True)
    parser.add_argument("--client-binary", type=Path, required=True)
    parser.add_argument("--scenarios-root", type=Path, default=Path("experiments/mitm/scenarios"))
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    args = parser.parse_args()

    if args.scenarios:
        scenarios = args.scenarios
    else:
        scenarios = scenario_names(args.scenarios_root, args.official)
        missing = [
            name
            for name in scenarios
            if not (args.scenarios_root / name / "scenario.toml").is_file()
        ]
        if missing:
            raise RuntimeError(
                "scenario names have no local manifests: " + ", ".join(missing)
            )
        print(
            f"runner-light: discovered {len(scenarios)} scenarios "
            f"({sum(name.startswith('2') for name in scenarios)} v2.337.0)"
        )

    token = secrets.token_hex(32)
    port = free_port()
    server_url = f"http://127.0.0.1:{port}"
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="preloop-runner-light-") as temp:
        temp_root = Path(temp)
        server_state = temp_root / "server-state"
        server_log = temp_root / "server.log"
        runner_root = temp_root / "runner"
        runner_log = temp_root / "runner.log"
        env = os.environ.copy()
        env.update(
            {
                "PRELOOP_PUBLIC_URL": server_url,
                "PRELOOP_SYSTEM_TOKEN": token,
                "PRELOOP_CONFIG": str(temp_root / "config.toml"),
                "PRELOOP_REGISTRATION_POLICY": "permissive",
                "RUST_LOG": os.environ.get("RUST_LOG", "warn"),
            }
        )
        with server_log.open("w") as log_file:
            server = subprocess.Popen(
                [
                    str(args.server_binary),
                    "serve",
                    "--listen",
                    f"127.0.0.1:{port}",
                    "--state-dir",
                    str(server_state),
                ],
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
        runner: subprocess.Popen[str] | None = None
        try:
            wait_for_health(server_url, server, server_log)
            configure_runner(args.runner_binary, runner_root, server_url, token)
            with runner_log.open("w") as log_file:
                runner = subprocess.Popen(
                    [str(args.runner_binary), "--runner-root", str(runner_root), "run"],
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                )

            records: list[dict[str, Any]] = []
            for scenario_name in scenarios:
                scenario = args.scenarios_root / scenario_name
                workflow, duration = scenario_workflow(scenario)
                run_id = submit(args.client_binary, server_url, token, workflow, scenario)
                run = wait_for_run(
                    server_url,
                    token,
                    run_id,
                    runner,
                    max(180, duration + 60),
                )
                result = semantic_result(run)
                records.append(
                    {
                        "runner": "preloop-local",
                        "workflow": scenario_name,
                        "run_id": run_id,
                        "conclusion": result["conclusion"],
                        "result": result,
                    }
                )
                print(f"runner-light: {scenario_name}: {result['conclusion']}")
            args.output.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        except Exception:
            print(f"runner-light: failure artifacts at {temp_root}", file=sys.stderr)
            for log in (server_log, runner_log):
                if log.exists():
                    print(log.read_text(errors="replace"), file=sys.stderr)
            raise
        finally:
            stop_process(runner)
            stop_process(server)

    print(f"runner-light: wrote {len(scenarios)} records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
