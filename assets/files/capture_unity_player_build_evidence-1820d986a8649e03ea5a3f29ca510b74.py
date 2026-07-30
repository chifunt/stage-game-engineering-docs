#!/usr/bin/env python3
"""Bind a fresh Unity player artifact to a clean committed project revision."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apply_unity_bootstrap import (
    BootstrapApplyFailure,
    is_within,
    require_external_path,
    write_json_atomic,
)
from plan_unity_bootstrap import (
    BootstrapPlanFailure,
    inspect_git,
    parse_project_version,
    require_unity_project,
    sha256_file,
)
from validate_unity_profile import ProfileValidationFailure, validate_schema

try:
    from strict_data import load_json as load_json_document
except ModuleNotFoundError:  # imported as a repository module in tests
    from scripts.strict_data import load_json as load_json_document


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SCHEMA = ROOT / "schemas" / "stage-unity-player-build-receipt-v0.1.schema.json"
EVIDENCE_SCHEMA = ROOT / "schemas" / "stage-unity-player-build-evidence-v0.1.schema.json"
EVIDENCE_VERSION = "0.1"
SUCCESS_MARKER = "Build Finished, Result: Success."
FAILURE_MARKERS = (
    "Build Finished, Result: Failure",
    "Build completed with a result of 'Failed'",
    "Aborting batchmode due to failure",
    "Scripts have compiler errors",
    "error CS",
)


class UnityPlayerBuildEvidenceFailure(Exception):
    """Raised when player-build evidence cannot be attributed safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write normalized evidence outside the target Git work tree.",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO-8601 timestamp for reproducible receipts; defaults to current UTC time.",
    )
    return parser.parse_args()


def load_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    try:
        data = path.read_bytes()
        value = load_json_document(data, json)
    except (OSError, ValueError) as exc:
        raise UnityPlayerBuildEvidenceFailure(f"Could not read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UnityPlayerBuildEvidenceFailure(f"{label} root must be an object: {path}")
    return value, hashlib.sha256(data).hexdigest()


def git_revision_timestamp(root: Path, revision: str) -> float:
    result = subprocess.run(
        ["git", "show", "-s", "--format=%ct", revision],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UnityPlayerBuildEvidenceFailure(
            result.stderr.strip() or f"Could not inspect commit time for {revision}."
        )
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise UnityPlayerBuildEvidenceFailure(
            f"Git returned an invalid commit time for {revision}."
        ) from exc


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def parsed_timestamp(value: str, label: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnityPlayerBuildEvidenceFailure(f"{label} must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise UnityPlayerBuildEvidenceFailure(f"{label} must include a timezone.")
    return parsed.timestamp()


def fresh_source(path: Path, revision_timestamp: float, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise UnityPlayerBuildEvidenceFailure(f"{label} does not exist: {path}")
    modified = path.stat().st_mtime
    if modified < revision_timestamp:
        raise UnityPlayerBuildEvidenceFailure(f"{label} predates the current Git revision: {path}")
    digest = sha256_file(path)
    if digest is None:
        raise UnityPlayerBuildEvidenceFailure(f"Could not hash {label}: {path}")
    return {
        "path": str(path),
        "sha256": digest,
        "modified_at": utc_timestamp(modified),
        "after_current_revision": True,
    }


def enabled_build_scenes(project: Path) -> tuple[list[str], str]:
    path = project / "ProjectSettings" / "EditorBuildSettings.asset"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnityPlayerBuildEvidenceFailure(
            f"Could not read Unity Editor Build Settings: {exc}"
        ) from exc

    scenes: list[str] = []
    pending_enabled: bool | None = None
    for line in text.splitlines():
        enabled_match = re.match(r"^\s*-\s+enabled:\s*([01])\s*$", line)
        if enabled_match:
            if pending_enabled is not None:
                raise UnityPlayerBuildEvidenceFailure(
                    "Editor Build Settings contains an entry without a path."
                )
            pending_enabled = enabled_match.group(1) == "1"
            continue
        if pending_enabled is None:
            continue
        path_match = re.match(r"^\s+path:\s*(.*?)\s*$", line)
        if not path_match:
            continue
        scene = path_match.group(1)
        if not scene:
            raise UnityPlayerBuildEvidenceFailure(
                "Editor Build Settings contains an empty scene path."
            )
        if pending_enabled:
            scenes.append(scene)
        pending_enabled = None

    if pending_enabled is not None:
        raise UnityPlayerBuildEvidenceFailure(
            "Editor Build Settings ends with an entry without a path."
        )
    if not scenes:
        raise UnityPlayerBuildEvidenceFailure("Editor Build Settings has no enabled scenes.")
    if len(scenes) != len(set(scenes)):
        raise UnityPlayerBuildEvidenceFailure(
            "Editor Build Settings contains duplicate enabled scenes."
        )
    for scene in scenes:
        relative = Path(scene)
        if relative.is_absolute() or ".." in relative.parts or not scene.startswith("Assets/"):
            raise UnityPlayerBuildEvidenceFailure(
                f"Enabled scene path is not a safe project-relative Assets path: {scene}"
            )
        if not (project / relative).is_file():
            raise UnityPlayerBuildEvidenceFailure(f"Enabled build scene does not exist: {scene}")

    digest = sha256_file(path)
    if digest is None:
        raise UnityPlayerBuildEvidenceFailure("Could not hash Unity Editor Build Settings.")
    return scenes, digest


def artifact_tree(path: Path, revision_timestamp: float) -> dict[str, Any]:
    if path.is_symlink():
        raise UnityPlayerBuildEvidenceFailure(
            f"Player artifact root must not be a symbolic link: {path}"
        )
    if not path.exists():
        raise UnityPlayerBuildEvidenceFailure(f"Player artifact does not exist: {path}")

    modified = path.stat().st_mtime
    if modified < revision_timestamp:
        raise UnityPlayerBuildEvidenceFailure(
            f"Player artifact predates the current Git revision: {path}"
        )

    digest = hashlib.sha256()
    file_count = 0
    directory_count = 0
    symlink_count = 0
    total_size = 0

    def add_record(*parts: object) -> None:
        digest.update("\0".join(str(part) for part in parts).encode("utf-8"))
        digest.update(b"\n")

    if path.is_file():
        content_hash = sha256_file(path)
        if content_hash is None:
            raise UnityPlayerBuildEvidenceFailure(f"Could not hash player artifact: {path}")
        info = path.stat()
        file_count = 1
        total_size = info.st_size
        add_record("file", ".", stat.S_IMODE(info.st_mode), info.st_size, content_hash)
        kind = "file"
    elif path.is_dir():
        kind = "directory"
        entries = sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix())
        for entry in entries:
            relative = entry.relative_to(path).as_posix()
            info = entry.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if entry.is_symlink():
                symlink_count += 1
                add_record("symlink", relative, mode, os.readlink(entry))
            elif entry.is_dir():
                directory_count += 1
                add_record("directory", relative, mode)
            elif entry.is_file():
                content_hash = sha256_file(entry)
                if content_hash is None:
                    raise UnityPlayerBuildEvidenceFailure(
                        f"Could not hash player artifact file: {entry}"
                    )
                file_count += 1
                total_size += info.st_size
                add_record("file", relative, mode, info.st_size, content_hash)
            else:
                raise UnityPlayerBuildEvidenceFailure(
                    f"Unsupported special file in player artifact: {entry}"
                )
    else:
        raise UnityPlayerBuildEvidenceFailure(
            f"Player artifact is neither a file nor a directory: {path}"
        )

    if file_count == 0 or total_size == 0:
        raise UnityPlayerBuildEvidenceFailure(
            f"Player artifact contains no non-empty file payload: {path}"
        )
    return {
        "path": str(path),
        "kind": kind,
        "exists": True,
        "modified_at": utc_timestamp(modified),
        "after_current_revision": True,
        "tree_sha256": digest.hexdigest(),
        "file_count": file_count,
        "directory_count": directory_count,
        "symlink_count": symlink_count,
        "total_size_bytes": total_size,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    project, _, _ = require_unity_project(args.project)
    git, status_sha256 = inspect_git(project)
    if not git["is_repository"] or not git["root"] or not git["revision"]:
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player-build evidence requires a committed Git work tree."
        )
    if git["dirty"]:
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player-build evidence requires a clean Git work tree."
        )
    if status_sha256 is None:
        raise UnityPlayerBuildEvidenceFailure("Could not fingerprint clean Git status.")

    protected_root = Path(git["root"]).resolve()
    receipt_path = require_external_path(
        args.receipt, protected_root, "Unity player-build source receipt"
    )
    log_path = require_external_path(args.log, protected_root, "Unity player-build log")
    if receipt_path == log_path:
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player-build receipt and log must be separate files."
        )

    receipt, receipt_sha256 = load_json(receipt_path, "Unity player-build receipt")
    validate_schema(receipt, SOURCE_SCHEMA, "Unity player-build source receipt")
    raw_output = Path(receipt["output_path"]).expanduser()
    if not raw_output.is_absolute():
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player-build receipt output_path must be absolute."
        )
    artifact_path = require_external_path(raw_output, protected_root, "Unity player artifact")
    if is_within(protected_root, artifact_path):
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player artifact must not contain the target Git work tree."
        )
    if is_within(receipt_path, artifact_path) or is_within(log_path, artifact_path):
        raise UnityPlayerBuildEvidenceFailure(
            "Build receipt and log must not be stored inside the player artifact."
        )

    if args.output:
        output = require_external_path(
            args.output, protected_root, "Unity player-build evidence output"
        )
        if output in {receipt_path, log_path}:
            raise UnityPlayerBuildEvidenceFailure(
                "Normalized evidence must not overwrite its source receipt or log."
            )
        if is_within(output, artifact_path):
            raise UnityPlayerBuildEvidenceFailure(
                "Normalized evidence must not be stored inside the player artifact."
            )

    revision_timestamp = git_revision_timestamp(protected_root, git["revision"])
    receipt_source = fresh_source(receipt_path, revision_timestamp, "Unity player-build receipt")
    if receipt_source["sha256"] != receipt_sha256:
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player-build receipt changed while it was being inspected."
        )
    log_source = fresh_source(log_path, revision_timestamp, "Unity player-build log")
    if parsed_timestamp(receipt["generated_at"], "Build receipt generated_at") < revision_timestamp:
        raise UnityPlayerBuildEvidenceFailure(
            "Unity player-build receipt was generated before the current Git revision."
        )

    editor_version, editor_revision = parse_project_version(project)
    if receipt["unity_version"] != editor_version:
        raise UnityPlayerBuildEvidenceFailure(
            "Build receipt Unity version does not match ProjectVersion.txt "
            f"({receipt['unity_version']} != {editor_version})."
        )
    scenes, build_settings_sha256 = enabled_build_scenes(project)
    if receipt["included_scenes"] != scenes:
        raise UnityPlayerBuildEvidenceFailure(
            "Build receipt scenes do not match current enabled Editor Build Settings."
        )
    if (
        receipt["result"] != "Succeeded"
        or receipt["total_errors"] != 0
        or not receipt["artifact_exists"]
        or receipt["total_size_bytes"] <= 0
    ):
        raise UnityPlayerBuildEvidenceFailure(
            "Build receipt does not describe a successful non-empty player build."
        )

    try:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise UnityPlayerBuildEvidenceFailure(f"Could not read build log: {exc}") from exc
    if SUCCESS_MARKER not in log_text:
        raise UnityPlayerBuildEvidenceFailure(
            "Build log does not contain Unity's successful build marker."
        )
    observed_failures = [marker for marker in FAILURE_MARKERS if marker in log_text]
    if observed_failures:
        raise UnityPlayerBuildEvidenceFailure(
            "Build log contains failure markers: " + ", ".join(observed_failures)
        )

    artifact = artifact_tree(artifact_path, revision_timestamp)
    project_version_sha256 = sha256_file(project / "ProjectSettings" / "ProjectVersion.txt")
    if project_version_sha256 is None:
        raise UnityPlayerBuildEvidenceFailure("Could not hash ProjectVersion.txt.")

    receipt_build = {
        "receipt_version": receipt["receipt_version"],
        "generated_at": receipt["generated_at"],
        "project_name": receipt["project_name"],
        "unity_version": receipt["unity_version"],
        "target": receipt["build_target"],
        "target_group": receipt["build_target_group"],
        "options": receipt["build_options"],
        "result": receipt["result"],
        "errors": receipt["total_errors"],
        "warnings": receipt["total_warnings"],
        "reported_size_bytes": receipt["total_size_bytes"],
        "duration_seconds": receipt["duration_seconds"],
        "guid": receipt["build_guid"],
        "included_scenes": receipt["included_scenes"],
    }
    evidence = {
        "build_evidence_version": EVIDENCE_VERSION,
        "generated_at": args.generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project": {
            "path": str(project),
            "name": project.name,
            "editor_version": editor_version,
            "editor_revision": editor_revision,
            "source_fingerprints": {
                "project_version": project_version_sha256,
                "editor_build_settings": build_settings_sha256,
            },
            "git": {
                "root": git["root"],
                "origin": git["origin"],
                "branch": git["branch"],
                "revision": git["revision"],
                "revision_committed_at": utc_timestamp(revision_timestamp),
                "dirty": False,
                "dirty_path_count": 0,
                "status_sha256": status_sha256,
            },
        },
        "source": {
            "receipt": receipt_source,
            "log": log_source,
            "build": receipt_build,
        },
        "artifact": artifact,
        "summary": {
            "status": "passed",
            "target": receipt["build_target"],
            "scene_count": len(scenes),
            "warning_count": receipt["total_warnings"],
            "blocking_findings": [],
            "launch_status": "not_assessed",
            "human_acceptance_status": "not_assessed",
            "interpretation": (
                "This receipt proves that the recorded clean revision produced the "
                "hashed Unity player artifact for the declared target and enabled "
                "scenes. It does not prove that the player launches, accepts input, "
                "performs acceptably, or is accepted for release."
            ),
        },
    }
    validate_schema(evidence, EVIDENCE_SCHEMA, "Unity player-build evidence")
    return evidence


def render_text(evidence: dict[str, Any]) -> str:
    summary = evidence["summary"]
    artifact = evidence["artifact"]
    return "\n".join(
        (
            f"STAGE Unity player-build evidence: {summary['status']}",
            f"Project: {evidence['project']['name']}",
            f"Revision: {evidence['project']['git']['revision']}",
            f"Target: {summary['target']}",
            f"Scenes: {summary['scene_count']}",
            f"Artifact: {artifact['path']}",
            f"Artifact SHA-256: {artifact['tree_sha256']}",
            "Launch: not assessed",
        )
    )


def main() -> int:
    args = parse_args()
    try:
        evidence = run(args)
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(output, evidence)
            print(f"Wrote Unity player-build evidence: {output}", file=sys.stderr)
        if args.format == "json":
            print(json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False))
        else:
            print(render_text(evidence))
        return 0
    except (
        UnityPlayerBuildEvidenceFailure,
        BootstrapApplyFailure,
        BootstrapPlanFailure,
        ProfileValidationFailure,
        OSError,
        ValueError,
    ) as exc:
        print(f"STAGE Unity player-build evidence capture failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
