#!/usr/bin/env python3
"""Launch an exact Unity player artifact and capture project-defined readiness evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apply_unity_bootstrap import (
    BootstrapApplyFailure,
    is_within,
    require_external_path,
    write_json_atomic,
)
from capture_unity_player_build_evidence import artifact_tree, load_json
from plan_unity_bootstrap import (
    BootstrapPlanFailure,
    inspect_git,
    parse_project_version,
    require_unity_project,
    sha256_file,
)
from validate_unity_profile import ProfileValidationFailure, validate_schema

ROOT = Path(__file__).resolve().parents[1]
BUILD_EVIDENCE_SCHEMA = ROOT / "schemas" / "stage-unity-player-build-evidence-v0.1.schema.json"
SOURCE_SCHEMA = ROOT / "schemas" / "stage-unity-player-launch-receipt-v0.1.schema.json"
EVIDENCE_SCHEMA = ROOT / "schemas" / "stage-unity-player-launch-evidence-v0.1.schema.json"
EVIDENCE_VERSION = "0.1"
RECEIPT_PATH_VARIABLE = "STAGE_UNITY_LAUNCH_RECEIPT_PATH"
TOKEN_VARIABLE = "STAGE_UNITY_LAUNCH_TOKEN"
FAILURE_MARKERS = (
    "NullReferenceException",
    "MissingReferenceException",
    "VContainerException",
    "Fatal Error",
    "Crash!!!",
    "Aborting batchmode",
    "Failed to initialize",
    "Scripts have compiler errors",
)


class UnityPlayerLaunchEvidenceFailure(Exception):
    """Raised when player-launch evidence cannot be established safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--build-evidence", type=Path, required=True)
    parser.add_argument("--executable-relative", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write normalized evidence outside the target Git work tree.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO-8601 timestamp for the normalized evidence; defaults to current UTC time.",
    )
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def parsed_timestamp(value: str, label: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnityPlayerLaunchEvidenceFailure(f"{label} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise UnityPlayerLaunchEvidenceFailure(f"{label} must include a timezone.")
    return parsed.timestamp()


def require_absent_external_file(
    value: Path,
    project_root: Path,
    artifact_root: Path,
    label: str,
) -> Path:
    path = require_external_path(value, project_root, label)
    if is_within(path, artifact_root):
        raise UnityPlayerLaunchEvidenceFailure(
            f"{label} must not be stored inside the player artifact."
        )
    if path.exists() or path.is_symlink():
        raise UnityPlayerLaunchEvidenceFailure(
            f"{label} already exists; choose a fresh path: {path}"
        )
    return path


def fresh_launch_source(path: Path, started_at: float, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise UnityPlayerLaunchEvidenceFailure(f"{label} does not exist: {path}")
    modified = path.stat().st_mtime
    if modified < started_at:
        raise UnityPlayerLaunchEvidenceFailure(
            f"{label} predates the player-launch operation: {path}"
        )
    digest = sha256_file(path)
    if digest is None:
        raise UnityPlayerLaunchEvidenceFailure(f"Could not hash {label}: {path}")
    return {
        "path": str(path),
        "sha256": digest,
        "modified_at": utc_timestamp(modified),
        "after_launch_started": True,
    }


def relative_executable(artifact: Path, value: str) -> tuple[Path, str]:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise UnityPlayerLaunchEvidenceFailure(
            "Player executable must be a safe path relative to the artifact root."
        )
    executable = (artifact / relative).resolve()
    if not is_within(executable, artifact):
        raise UnityPlayerLaunchEvidenceFailure("Player executable escapes the artifact root.")
    if executable.is_symlink() or not executable.is_file():
        raise UnityPlayerLaunchEvidenceFailure(
            f"Player executable is not a regular file: {executable}"
        )
    if os.name != "nt" and not os.access(executable, os.X_OK):
        raise UnityPlayerLaunchEvidenceFailure(f"Player executable is not executable: {executable}")
    return executable, relative.as_posix()


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:  # pragma: no cover - exercised on Windows hosts
        process.kill()
    process.communicate()


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.timeout_seconds <= 0:
        raise UnityPlayerLaunchEvidenceFailure("timeout-seconds must be positive.")

    project, _, _ = require_unity_project(args.project)
    git, status_sha256 = inspect_git(project)
    if not git["is_repository"] or not git["root"] or not git["revision"]:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch evidence requires a committed Git work tree."
        )
    if git["dirty"]:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch evidence requires a clean Git work tree."
        )
    if status_sha256 is None:
        raise UnityPlayerLaunchEvidenceFailure("Could not fingerprint clean Git status.")

    project_root = Path(git["root"]).resolve()
    build_path = require_external_path(
        args.build_evidence, project_root, "Unity player-build evidence"
    )
    build, build_sha256 = load_json(build_path, "Unity player-build evidence")
    validate_schema(build, BUILD_EVIDENCE_SCHEMA, "Unity player-build evidence")
    if build["project"]["git"]["revision"] != git["revision"]:
        raise UnityPlayerLaunchEvidenceFailure(
            "Player-build evidence revision does not match the current clean project."
        )
    if build["project"]["git"]["status_sha256"] != status_sha256:
        raise UnityPlayerLaunchEvidenceFailure(
            "Player-build evidence clean-status fingerprint does not match the project."
        )

    editor_version, _ = parse_project_version(project)
    if build["project"]["editor_version"] != editor_version:
        raise UnityPlayerLaunchEvidenceFailure(
            "Player-build evidence Unity version does not match the current project."
        )

    artifact = require_external_path(
        Path(build["artifact"]["path"]), project_root, "Unity player artifact"
    )
    if is_within(build_path, artifact):
        raise UnityPlayerLaunchEvidenceFailure(
            "Player-build evidence must not be stored inside the player artifact."
        )
    revision_time_result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", git["revision"]],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if revision_time_result.returncode != 0:
        raise UnityPlayerLaunchEvidenceFailure(
            revision_time_result.stderr.strip() or "Could not inspect source revision time."
        )
    revision_timestamp = float(revision_time_result.stdout.strip())
    before_artifact = artifact_tree(artifact, revision_timestamp)
    for key in (
        "tree_sha256",
        "file_count",
        "directory_count",
        "symlink_count",
        "total_size_bytes",
    ):
        if before_artifact[key] != build["artifact"][key]:
            raise UnityPlayerLaunchEvidenceFailure(
                f"Player artifact no longer matches build evidence ({key})."
            )

    executable, executable_relative = relative_executable(artifact, args.executable_relative)
    receipt_path = require_absent_external_file(
        args.receipt, project_root, artifact, "Unity player-launch receipt"
    )
    log_path = require_absent_external_file(
        args.log, project_root, artifact, "Unity player-launch log"
    )
    if receipt_path == log_path:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt and log must be separate files."
        )
    output_path: Path | None = None
    if args.output:
        output_path = require_absent_external_file(
            args.output,
            project_root,
            artifact,
            "Unity player-launch evidence output",
        )
        if output_path in {receipt_path, log_path, build_path}:
            raise UnityPlayerLaunchEvidenceFailure(
                "Normalized launch evidence must not overwrite a source artifact."
            )

    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    token = secrets.token_hex(16)
    environment = os.environ.copy()
    environment[RECEIPT_PATH_VARIABLE] = str(receipt_path)
    environment[TOKEN_VARIABLE] = token
    command = [str(executable), "-batchmode", "-logFile", str(log_path)]
    started_at = time.time()
    process = subprocess.Popen(
        command,
        cwd=artifact,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        terminate_process(process)
        raise UnityPlayerLaunchEvidenceFailure(
            f"Unity player launch exceeded {args.timeout_seconds:g} seconds."
        ) from exc
    completed_at = time.time()
    if process.returncode != 0:
        raise UnityPlayerLaunchEvidenceFailure(
            f"Unity player exited with code {process.returncode}."
        )

    receipt_source = fresh_launch_source(receipt_path, started_at, "Unity player-launch receipt")
    log_source = fresh_launch_source(log_path, started_at, "Unity player-launch log")
    receipt, receipt_sha256 = load_json(receipt_path, "Unity player-launch receipt")
    if receipt_source["sha256"] != receipt_sha256:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt changed while it was being inspected."
        )
    validate_schema(receipt, SOURCE_SCHEMA, "Unity player-launch source receipt")
    if receipt["launch_token"] != token:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt token does not match this process."
        )
    generated_at = parsed_timestamp(receipt["generated_at"], "Launch receipt generated_at")
    if generated_at < started_at:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt was generated before this process started."
        )
    if receipt["project_name"] != build["source"]["build"]["project_name"]:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt project name does not match build evidence."
        )
    if receipt["unity_version"] != editor_version:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt Unity version does not match the project."
        )
    if receipt["active_scene"] not in build["source"]["build"]["included_scenes"]:
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt active scene was not included in the build."
        )
    check_names = [check["name"] for check in receipt["checks"]]
    if len(check_names) != len(set(check_names)):
        raise UnityPlayerLaunchEvidenceFailure(
            "Unity player-launch receipt contains duplicate check names."
        )
    failed_checks = [check["name"] for check in receipt["checks"] if not check["passed"]]
    if receipt["result"] != "Passed" or failed_checks:
        detail = ", ".join(failed_checks) or receipt["result"]
        raise UnityPlayerLaunchEvidenceFailure(f"Unity player-launch probe did not pass: {detail}")

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    observed_failures = [marker for marker in FAILURE_MARKERS if marker in log_text]
    if observed_failures:
        raise UnityPlayerLaunchEvidenceFailure(
            "Player log contains failure markers: " + ", ".join(observed_failures)
        )

    after_artifact = artifact_tree(artifact, revision_timestamp)
    if after_artifact["tree_sha256"] != before_artifact["tree_sha256"]:
        raise UnityPlayerLaunchEvidenceFailure(
            "Player artifact changed during launch; launch evidence is not attributable."
        )

    probe = {
        "receipt_version": receipt["receipt_version"],
        "generated_at": receipt["generated_at"],
        "name": receipt["probe_name"],
        "project_name": receipt["project_name"],
        "unity_version": receipt["unity_version"],
        "platform": receipt["platform"],
        "active_scene": receipt["active_scene"],
        "result": receipt["result"],
        "frames_observed": receipt["frames_observed"],
        "runtime_seconds": receipt["runtime_seconds"],
        "checks": receipt["checks"],
    }
    evidence = {
        "launch_evidence_version": EVIDENCE_VERSION,
        "generated_at": args.generated_at or utc_now(),
        "project": {
            "path": str(project),
            "name": project.name,
            "editor_version": editor_version,
            "git": {
                "root": git["root"],
                "origin": git["origin"],
                "branch": git["branch"],
                "revision": git["revision"],
                "dirty": False,
                "dirty_path_count": 0,
                "status_sha256": status_sha256,
            },
        },
        "build": {
            "evidence_path": str(build_path),
            "evidence_sha256": build_sha256,
            "evidence_version": build["build_evidence_version"],
            "target": build["summary"]["target"],
            "included_scenes": build["source"]["build"]["included_scenes"],
            "artifact_path": str(artifact),
            "artifact_tree_sha256": after_artifact["tree_sha256"],
            "artifact_file_count": after_artifact["file_count"],
            "artifact_size_bytes": after_artifact["total_size_bytes"],
        },
        "execution": {
            "executable_relative_path": executable_relative,
            "arguments": ["-batchmode", "-logFile", "<external-log>"],
            "started_at": utc_timestamp(started_at),
            "completed_at": utc_timestamp(completed_at),
            "duration_seconds": completed_at - started_at,
            "timeout_seconds": args.timeout_seconds,
            "timed_out": timed_out,
            "exit_code": process.returncode,
            "launch_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        },
        "source": {
            "receipt": receipt_source,
            "log": log_source,
            "probe": probe,
        },
        "summary": {
            "status": "passed",
            "process_status": "exited_zero",
            "scene_ready_status": "passed",
            "input_status": "not_assessed",
            "visual_status": "not_assessed",
            "performance_status": "not_assessed",
            "human_acceptance_status": "not_assessed",
            "blocking_findings": [],
            "interpretation": (
                "The exact revision-bound player artifact started, loaded an included "
                "scene, passed the project's named readiness checks, and exited cleanly. "
                "This does not establish real input, visual correctness, representative "
                "performance, or human release acceptance."
            ),
        },
    }
    validate_schema(evidence, EVIDENCE_SCHEMA, "Unity player-launch evidence")
    if output_path is not None:
        write_json_atomic(output_path, evidence)
    return evidence


def render_text(evidence: dict[str, Any]) -> str:
    return "\n".join(
        (
            f"STAGE Unity player-launch evidence: {evidence['summary']['status']}",
            f"Project: {evidence['project']['name']}",
            f"Revision: {evidence['project']['git']['revision']}",
            f"Artifact SHA-256: {evidence['build']['artifact_tree_sha256']}",
            f"Scene: {evidence['source']['probe']['active_scene']}",
            f"Checks: {len(evidence['source']['probe']['checks'])}",
            "Input, visuals, performance, and human acceptance: not assessed",
        )
    )


def main() -> int:
    args = parse_args()
    try:
        evidence = run(args)
        if args.output:
            print(f"Wrote Unity player-launch evidence: {args.output.resolve()}", file=sys.stderr)
        if args.format == "json":
            print(json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False))
        else:
            print(render_text(evidence))
        return 0
    except (
        UnityPlayerLaunchEvidenceFailure,
        BootstrapApplyFailure,
        BootstrapPlanFailure,
        ProfileValidationFailure,
        OSError,
        ValueError,
    ) as exc:
        print(f"STAGE Unity player-launch probe failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
