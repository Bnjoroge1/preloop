#!/usr/bin/env python3
"""Fail unless MITM scenarios have usable, owned runner flow captures."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "experiments" / "mitm" / "scenarios"
GOLDENS = ROOT / ".runner-watch" / "golden"


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exclude-prefix", action="append", default=[], help="omit manifest names with this prefix")
    parser.add_argument("--validate-ownership", action="store_true", help="validate acquirejob file/job ownership and plan GUID uniqueness")
    parser.add_argument("--version", help="runner version; defaults to versions.toml")
    parser.add_argument("--cell", help="capture cell, for example gh-official")
    parser.add_argument(
        "--golden-root",
        type=Path,
        help="version directory containing direct scenarios or capture cells",
    )
    parser.add_argument(
        "--scenario-prefix",
        default="",
        help="only check scenario manifests whose names start with this prefix",
    )
    return parser.parse_args()


def load_version() -> tuple[str, str]:
    version = tomllib.loads((ROOT / "versions.toml").read_text())["runner_version"]
    mitm_version = tomllib.loads(
        (ROOT / "experiments" / "mitm" / "versions.toml").read_text()
    )["runner_version"]
    return version, mitm_version


def capture_root(root: Path, cell: str | None) -> Path:
    if cell:
        root = root / cell
    if not root.exists():
        raise SystemExit(f"missing golden root: {root}")
    if not cell and not any(root.glob("*/flows.jsonl")):
        cells = sorted(path.name for path in root.iterdir() if path.is_dir())
        if cells:
            raise SystemExit(
                f"golden root has capture cells ({', '.join(cells)}); pass --cell"
            )
    return root


def acquire_payloads(flows_path: Path) -> list[dict[str, Any]]:
    payloads = []
    for line in flows_path.read_text().splitlines():
        try:
            flow = json.loads(line)
            if not flow.get("path", "").endswith("/acquirejob"):
                continue
            encoded = flow.get("response_body_b64")
            if not encoded:
                continue
            body = json.loads(base64.b64decode(encoded))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if body.get("steps") is not None and body.get("fileTable"):
            payloads.append(body)
    return payloads


def workflow_declarations(path: Path) -> tuple[set[str], set[str]]:
    """Read the generated/simple workflow's two-space job declarations."""
    ids: set[str] = set()
    names: set[str] = set()
    in_jobs = False
    current: str | None = None
    for line in path.read_text().splitlines():
        if line == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        match = re.fullmatch(r"  ([A-Za-z0-9_.-]+):", line)
        if match:
            current = match.group(1)
            ids.add(current)
            continue
        if current and (match := re.fullmatch(r"    name: (.+)", line)):
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] == '"':
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value[1:-1]
            names.add(value)
    return ids, names


def normalize_job_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower() or "reconstructed"


def check_ownership(scenario: str, payloads: list[dict[str, Any]]) -> list[str]:
    if not payloads:
        return []
    workflow = SCENARIOS / scenario / f"{scenario}.yml"
    if not workflow.exists():
        return [f"{scenario}: acquired job has no workflow manifest"]
    ids, names = workflow_declarations(workflow)
    errors: list[str] = []
    expected_file = f"{scenario}.yml"
    for payload in payloads:
        files = {Path(str(item)).name for item in payload.get("fileTable", [])}
        if expected_file not in files:
            errors.append(
                f"{scenario}: fileTable does not contain {expected_file!r}: {sorted(files)}"
            )
        display = str(payload.get("jobDisplayName", ""))
        base = display.split(" (", 1)[0]
        if (
            display not in names
            and base not in ids
            and base not in names
            and normalize_job_id(base) not in ids
        ):
            errors.append(
                f"{scenario}: acquired job {display!r} is not declared by its workflow"
            )
    return errors


def check_plans(label: str, payloads: list[dict[str, Any]], owners: dict[str, str], errors: list[str]) -> None:
    for payload in payloads:
        plan_id = payload.get("plan", {}).get("planId")
        if not plan_id:
            continue
        previous = owners.setdefault(str(plan_id), label)
        if previous != label:
            errors.append(f"plan {plan_id} shared by captures {previous} and {label}")


def main() -> int:
    options = args()
    pinned_version, mitm_version = load_version()
    version = options.version or pinned_version
    if pinned_version != mitm_version and not options.version:
        raise SystemExit(
            f"runner pins differ: versions.toml={pinned_version}, "
            f"experiments/mitm={mitm_version}"
        )
    root = options.golden_root or (GOLDENS / f"v{version}")
    root = capture_root(root, options.cell)
    expected = {
        path.parent.name
        for path in SCENARIOS.glob("*/scenario.toml")
        if path.parent.name.startswith(options.scenario_prefix)
        and not any(path.parent.name.startswith(prefix) for prefix in options.exclude_prefix)
    }
    replayable = {
        path.parent.name
        for path in root.glob("*/flows.jsonl")
        if path.stat().st_size > 0 and path.parent.name in expected
    }
    missing = sorted(expected - replayable)
    if missing:
        raise SystemExit(
            f"missing v{version} MITM flows ({len(missing)}): " + ", ".join(missing)
        )

    invalid: list[str] = []
    owners: dict[str, str] = {}
    for scenario in sorted(expected):
        capture = root / scenario
        flows_path = capture / "flows.jsonl"
        summary_path = capture / "summary.json"
        try:
            summary = json.loads(summary_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            invalid.append(f"{scenario}: invalid summary")
            continue
        flow_count = 0
        mitm_probe_count = 0
        for flow_line in flows_path.read_text().splitlines():
            try:
                flow = json.loads(flow_line)
            except json.JSONDecodeError:
                continue
            flow_count += 1
            mitm_probe_count += flow.get("host") == "mitm.it"
        valid_counts = {flow_count}
        if mitm_probe_count:
            valid_counts.add(flow_count - mitm_probe_count)
        if (
            summary.get("status") != "ok"
            or summary.get("runner_version") != version
            or summary.get("flows_count") not in valid_counts
        ):
            invalid.append(
                f"{scenario}: status={summary.get('status')!r} "
                f"version={summary.get('runner_version')!r} "
                f"flows={summary.get('flows_count')!r}/{sorted(valid_counts)}"
            )
        if options.validate_ownership:
            payloads = acquire_payloads(flows_path)
            invalid.extend(check_ownership(scenario, payloads[:1]))
            check_plans(f"{options.cell or 'direct'}/{scenario}", payloads, owners, invalid)
    if invalid:
        raise SystemExit("unusable MITM captures: " + "; ".join(invalid))

    print(
        f"MITM corpus: {len(expected)}/{len(expected)} scenarios at runner v{version}"
        f" ({options.cell or 'direct'}; unique plans={len(owners)})"
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
