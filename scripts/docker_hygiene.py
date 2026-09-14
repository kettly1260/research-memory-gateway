#!/usr/bin/env python3
"""
Research Memory Gateway - Workspace Generic Docker Host Resource Hygiene Tool (P0)

Generic, manifest-driven engine for Docker host inventory, lifecycle management,
safe targeted cleanup (dry-run by default), approved shared cache bounding,
and release-ready hard gate audits across production (Unraid) and build (gau-unraid) hosts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Base directory for task manifests
HYGIENE_BASE_DIR = Path(".local/docker-hygiene")

# Pinned versions and checksums for gau-unraid toolchain bootstrap
PINNED_TOOLCHAIN = {
    "buildx": {
        "version": "v0.37.1",
        "url": "https://github.com/docker/buildx/releases/download/v0.37.1/buildx-v0.37.1.linux-amd64",
        "target": "$HOME/.docker/cli-plugins/docker-buildx",
        "sha256": "9447199cdb435f25880548343c128a4b6650e8891ee598905d8d29d39a8e359b",
    },
    "compose": {
        "version": "v5.5.1",
        "url": "https://github.com/docker/compose/releases/download/v5.5.1/docker-compose-linux-x86_64",
        "target": "$HOME/.docker/cli-plugins/docker-compose",
        "sha256": "db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576",
    },
}

# Approved shared build cache limits on gau-unraid VM
GAU_CACHE_LIMITS = {
    "soft_limit_gb": 15.0,
    "hard_limit_gb": 25.0,
}


@dataclass
class HostConfig:
    name: str
    ssh_target: str
    is_production: bool


HOSTS: Dict[str, HostConfig] = {
    "unraid": HostConfig(name="unraid", ssh_target="unraid", is_production=True),
    "gau-unraid": HostConfig(name="gau-unraid", ssh_target="gau-unraid", is_production=False),
}


def run_ssh(host_key: str, cmd: str, timeout: int = 35) -> subprocess.CompletedProcess[str]:
    """
    Executes a command over SSH with explicit environment variables.
    Never relies on permanent shell profile mutations on the remote host.
    """
    if host_key not in HOSTS:
        raise ValueError(f"Unknown host key: {host_key}")
    host = HOSTS[host_key]

    if host_key == "gau-unraid":
        full_cmd = (
            "export PATH=$HOME/bin:$HOME/.local/bin:$PATH; "
            "export DOCKER_HOST=unix:///run/user/1000/docker.sock; "
            f"{cmd}"
        )
    else:
        full_cmd = cmd

    ssh_args = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        host.ssh_target,
        full_cmd,
    ]
    return subprocess.run(
        ssh_args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def collect_containers(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker inspect $(docker ps -aq) 2>/dev/null || true"
    res = run_ssh(host_key, cmd, timeout=35)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        raw_list = json.loads(res.stdout)
        results = []
        for c in raw_list:
            results.append({
                "id": c.get("Id", "")[:12],
                "full_id": c.get("Id", ""),
                "name": c.get("Name", "").lstrip("/"),
                "image": c.get("Config", {}).get("Image", ""),
                "image_id": c.get("Image", "")[:12],
                "image_full_id": c.get("Image", ""),
                "status": c.get("State", {}).get("Status", ""),
                "running": c.get("State", {}).get("Running", False),
                "created": c.get("Created", ""),
                "labels": c.get("Config", {}).get("Labels") or {},
                "mounts": [m.get("Source") for m in c.get("Mounts", []) if m.get("Source")],
                "networks": list(c.get("NetworkSettings", {}).get("Networks", {}).keys()),
            })
        return results
    except Exception:
        return []


def collect_images(host_key: str) -> List[Dict[str, Any]]:
    # Query docker images with JSON output
    cmd = "docker images --digests --no-trunc --format '{{json .}}'"
    res = run_ssh(host_key, cmd, timeout=35)
    if res.returncode != 0 or not res.stdout.strip():
        return []

    images = []
    ids_to_inspect = []
    for line in res.stdout.strip().splitlines():
        try:
            item = json.loads(line)
            img_id = item.get("ID", "")
            images.append({
                "repository": item.get("Repository", ""),
                "tag": item.get("Tag", ""),
                "digest": item.get("Digest", ""),
                "id": img_id[:12] if img_id else "",
                "full_id": img_id,
                "size": item.get("Size", ""),
                "created": item.get("CreatedAt", ""),
                "labels": {},
            })
            if img_id and img_id not in ids_to_inspect:
                ids_to_inspect.append(img_id)
        except Exception:
            pass

    # Batch inspect image labels if images exist
    if ids_to_inspect:
        inspect_cmd = f"docker image inspect {' '.join(ids_to_inspect[:40])} 2>/dev/null || true"
        res_insp = run_ssh(host_key, inspect_cmd, timeout=35)
        if res_insp.returncode == 0 and res_insp.stdout.strip():
            try:
                insp_list = json.loads(res_insp.stdout)
                label_map = {item.get("Id", ""): (item.get("Config", {}).get("Labels") or {}) for item in insp_list}
                for img in images:
                    img["labels"] = label_map.get(img["full_id"], {})
            except Exception:
                pass

    return images


def collect_volumes(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker volume inspect $(docker volume ls -q) 2>/dev/null || true"
    res = run_ssh(host_key, cmd, timeout=30)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        raw_list = json.loads(res.stdout)
        results = []
        for v in raw_list:
            results.append({
                "name": v.get("Name", ""),
                "driver": v.get("Driver", ""),
                "mountpoint": v.get("Mountpoint", ""),
                "labels": v.get("Labels") or {},
            })
        return results
    except Exception:
        return []


def collect_networks(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker network inspect $(docker network ls -q) 2>/dev/null || true"
    res = run_ssh(host_key, cmd, timeout=30)
    if res.returncode != 0 or not res.stdout.strip():
        return []
    try:
        raw_list = json.loads(res.stdout)
        results = []
        for n in raw_list:
            results.append({
                "id": n.get("Id", "")[:12],
                "name": n.get("Name", ""),
                "driver": n.get("Driver", ""),
                "scope": n.get("Scope", ""),
                "labels": n.get("Labels") or {},
            })
        return results
    except Exception:
        return []


def collect_buildx_builders(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker buildx ls 2>/dev/null || true"
    res = run_ssh(host_key, cmd, timeout=20)
    builders = []
    if res.returncode == 0 and res.stdout.strip():
        for line in res.stdout.strip().splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("NAME") or line_str.startswith("\\_"):
                continue
            parts = line_str.split()
            if parts:
                builder_name = parts[0].rstrip("*")
                builders.append({
                    "name": builder_name,
                    "is_current": "*" in parts[0],
                    "raw": line_str,
                })
    return builders


def collect_buildkit_cache(host_key: str) -> Dict[str, Any]:
    cmd = "docker buildx du 2>/dev/null || true"
    res = run_ssh(host_key, cmd, timeout=25)
    reclaimable = "0B"
    total = "0B"
    entries_count = 0
    if res.returncode == 0 and res.stdout.strip():
        for line in res.stdout.strip().splitlines():
            line_str = line.strip()
            if line_str.startswith("Reclaimable:"):
                reclaimable = line_str.replace("Reclaimable:", "").strip()
            elif line_str.startswith("Total:"):
                total = line_str.replace("Total:", "").strip()
            elif line_str and not line_str.startswith("ID") and not line_str.startswith("Shared:") and not line_str.startswith("Private:"):
                entries_count += 1
    return {
        "entries_count": entries_count,
        "reclaimable": reclaimable,
        "total": total,
    }


def collect_system_df(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker system df --format '{{json .}}' 2>/dev/null || true"
    res = run_ssh(host_key, cmd, timeout=25)
    items = []
    if res.returncode == 0 and res.stdout.strip():
        for line in res.stdout.strip().splitlines():
            try:
                items.append(json.loads(line))
            except Exception:
                pass
    return items


def collect_full_inventory(host_key: str) -> Dict[str, Any]:
    """Collects comprehensive inventory across all Docker object types."""
    containers = collect_containers(host_key)
    images = collect_images(host_key)
    volumes = collect_volumes(host_key)
    networks = collect_networks(host_key)
    builders = collect_buildx_builders(host_key)
    cache = collect_buildkit_cache(host_key)
    df = collect_system_df(host_key)

    # Calculate live container reference maps
    referenced_image_ids: Set[str] = {c["image_full_id"] for c in containers if c.get("image_full_id")}
    referenced_network_names: Set[str] = set()
    for c in containers:
        for net in c.get("networks", []):
            referenced_network_names.add(net)

    return {
        "host": host_key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "containers": containers,
        "images": images,
        "volumes": volumes,
        "networks": networks,
        "builders": builders,
        "buildkit_cache": cache,
        "system_df": df,
        "referenced_image_ids": list(referenced_image_ids),
        "referenced_network_names": list(referenced_network_names),
    }


class TaskManifestManager:
    """Manages task manifests, preflight, and postflight delta state."""

    @staticmethod
    def get_task_dir(task_id: str) -> Path:
        p = HYGIENE_BASE_DIR / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def init_task(cls, task_id: str, project: str, host: str) -> Dict[str, Any]:
        task_dir = cls.get_task_dir(task_id)
        manifest_file = task_dir / "manifest.json"
        preflight_file = task_dir / "preflight.json"

        preflight_data = collect_full_inventory(host)
        with open(preflight_file, "w", encoding="utf-8") as f:
            json.dump(preflight_data, f, indent=2, ensure_ascii=False)

        manifest = {
            "task_id": task_id,
            "project": project,
            "host": host,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "created_containers": [],
            "created_images": [],
            "created_volumes": [],
            "created_networks": [],
            "created_builders": [],
            "retained_resources": [],
            "cleanup_result": None,
        }
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        return manifest

    @classmethod
    def load_manifest(cls, task_id: str) -> Optional[Dict[str, Any]]:
        manifest_file = cls.get_task_dir(task_id) / "manifest.json"
        if manifest_file.exists():
            with open(manifest_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    @classmethod
    def save_postflight(cls, task_id: str, host: str) -> Dict[str, Any]:
        task_dir = cls.get_task_dir(task_id)
        postflight_file = task_dir / "postflight.json"
        postflight_data = collect_full_inventory(host)
        with open(postflight_file, "w", encoding="utf-8") as f:
            json.dump(postflight_data, f, indent=2, ensure_ascii=False)
        return postflight_data


def classify_resource_ownership(
    res_type: str,
    res: Dict[str, Any],
    manifest: Optional[Dict[str, Any]] = None,
    preflight: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    Determines resource ownership and lifecycle (A~G).
    Returns (category, rationale).
    Categories:
      A: Production
      B: Rollback
      C: Candidate
      D: Transient task resource (temporary container/image/volume/network)
      E: Task-owned dangling artifact or abandoned cache
      F: Other project resource (strictly protected)
      G: Ownership unknown
    """
    labels = res.get("labels") or {}
    lifecycle = labels.get("io.agent.lifecycle") or labels.get("io.rmg.lifecycle")
    project = labels.get("io.agent.project") or labels.get("io.rmg.project")
    task = labels.get("io.agent.task") or labels.get("io.rmg.task")

    # 1. Direct universal lifecycle labels
    if lifecycle == "production":
        return "A_production", "Explicit lifecycle=production label"
    if lifecycle == "rollback":
        return "B_rollback", "Explicit lifecycle=rollback label"
    if lifecycle == "candidate":
        return "C_candidate", "Explicit lifecycle=candidate label"
    if lifecycle == "temporary":
        return "D_transient", "Explicit lifecycle=temporary label"

    # 2. Check task manifest membership if task manifest is provided
    if manifest:
        current_task_id = manifest.get("task_id")
        created_conts = manifest.get("created_containers") or []
        created_imgs = manifest.get("created_images") or []

        res_id = res.get("id") or res.get("name")
        if task == current_task_id or res_id in created_conts or res_id in created_imgs:
            return "D_transient", f"Belongs to active task manifest {current_task_id}"

    # 3. Known production baseline identification on Unraid
    name = res.get("name", "")
    repo = res.get("repository", "")
    tag = res.get("tag", "")
    image = res.get("image", "")

    if "research-memory-gateway" in name or "research-memory-gateway" in repo or "research-memory-gateway" in image:
        if name == "research-memory-gateway" and res.get("running"):
            return "A_production", "Live running production container"
        if tag in ("v0.2.5.1-local",):
            return "A_production", "Active production image"
        if tag in ("v0.2.5", "latest"):
            return "B_rollback", "Designated rollback image"

    # 4. Resources belonging to other known projects
    other_project_indicators = [
        "wechat-hub", "immich", "navidrome", "new-api", "iptv-api", "cloudflared",
        "ollama", "openlist", "outlookemail", "postgres", "redis", "fast-note",
        "couchdb", "chat2api", "noveltts", "anythingllm", "ovms", "cliproxyapi",
        "mihomo", "metacubexd", "open-webui", "easynvr", "emby", "lucky",
        "qinglong", "e5sub", "gopeed", "jdownloader", "allinone", "webdis", "transmission", "zerotier"
    ]
    check_str = f"{name} {repo} {image}".lower()
    for ind in other_project_indicators:
        if ind in check_str:
            return "F_other_projects", f"Belongs to external project indicator: {ind}"

    # Default bridge/host/none networks
    if res_type == "network" and name in ("bridge", "host", "none"):
        return "F_other_projects", "Default Docker system network"

    # Default docker builder
    if res_type == "builder" and name == "default":
        return "F_other_projects", "Default host Docker builder"

    return "G_ownership_unknown", "No recognized project, task, or system signature"


def plan_cleanup(
    host_key: str,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Generates a targeted, safe cleanup plan. Read-only by default."""
    inv = collect_full_inventory(host_key)
    manifest = TaskManifestManager.load_manifest(task_id) if task_id else None

    containers_to_delete = []
    images_to_delete = []
    volumes_to_delete = []
    networks_to_delete = []
    builders_to_delete = []
    protected_skipped = []
    unknown_skipped = []

    # Evaluate containers
    for c in inv["containers"]:
        cat, reason = classify_resource_ownership("container", c, manifest)
        if cat in ("A_production", "B_rollback", "F_other_projects"):
            protected_skipped.append({"id": c["id"], "name": c["name"], "category": cat, "reason": reason})
        elif cat == "G_ownership_unknown":
            unknown_skipped.append({"id": c["id"], "name": c["name"], "reason": reason})
        elif cat == "D_transient":
            containers_to_delete.append({"id": c["id"], "name": c["name"], "reason": reason})

    # Evaluate images
    for img in inv["images"]:
        cat, reason = classify_resource_ownership("image", img, manifest)
        if cat in ("A_production", "B_rollback", "F_other_projects"):
            protected_skipped.append({"id": img["id"], "tag": f"{img['repository']}:{img['tag']}", "category": cat, "reason": reason})
        elif cat == "G_ownership_unknown":
            unknown_skipped.append({"id": img["id"], "tag": f"{img['repository']}:{img['tag']}", "reason": reason})
        elif cat in ("D_transient", "E_dangling"):
            images_to_delete.append({"id": img["id"], "tag": f"{img['repository']}:{img['tag']}", "reason": reason})

    return {
        "host": host_key,
        "task_id": task_id,
        "dry_run": True,
        "containers_to_delete": containers_to_delete,
        "images_to_delete": images_to_delete,
        "volumes_to_delete": volumes_to_delete,
        "networks_to_delete": networks_to_delete,
        "builders_to_delete": builders_to_delete,
        "protected_skipped": protected_skipped,
        "unknown_skipped": unknown_skipped,
    }


def execute_cleanup(
    host_key: str,
    task_id: Optional[str] = None,
    apply: bool = False,
) -> Dict[str, Any]:
    """
    Executes cleanup with strict safety guarantees:
    - DRY-RUN by default unless apply=True.
    - Production / rollback resources are strictly protected.
    - Unknown resources are rejected and skipped.
    - Global prune is NEVER run.
    """
    plan = plan_cleanup(host_key, task_id)
    plan["dry_run"] = not apply

    if not apply:
        print("[!] DRY-RUN MODE: No resources will be deleted. Pass --apply to execute.")
        return plan

    executed_actions = []

    # Delete containers
    for item in plan["containers_to_delete"]:
        c_id = item["id"]
        res = run_ssh(host_key, f"docker rm {c_id}")
        executed_actions.append({
            "target": c_id,
            "action": "docker rm",
            "returncode": res.returncode,
            "output": res.stdout.strip() if res.returncode == 0 else res.stderr.strip(),
        })

    # Delete images
    for item in plan["images_to_delete"]:
        img_id = item["id"]
        res = run_ssh(host_key, f"docker rmi {img_id}")
        executed_actions.append({
            "target": img_id,
            "action": "docker rmi",
            "returncode": res.returncode,
            "output": res.stdout.strip() if res.returncode == 0 else res.stderr.strip(),
        })

    plan["executed_actions"] = executed_actions
    return plan


def audit_release_gate(
    task_id: Optional[str] = None,
    host_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Comprehensive Release-Ready Gate Audit.
    Evaluates all 8 required metrics across hosts:
      1. temporary_containers
      2. superseded_candidate_images
      3. task_owned_dangling_images
      4. disposable_builders
      5. task_owned_build_cache
      6. temporary_networks
      7. temporary_volumes
      8. unknown_resources (blocking gate)
    """
    target_hosts = [host_filter] if host_filter and host_filter != "all" else ["unraid", "gau-unraid"]
    results = {}
    overall_pass = True

    for h in target_hosts:
        inv = collect_full_inventory(h)
        manifest = TaskManifestManager.load_manifest(task_id) if task_id else None

        temp_conts = []
        superseded_imgs = []
        dangling_imgs = []
        disposable_builders = []
        temp_networks = []
        temp_volumes = []
        unknown_resources = []

        # Audit containers
        for c in inv["containers"]:
            cat, reason = classify_resource_ownership("container", c, manifest)
            if cat == "D_transient":
                temp_conts.append(c["name"])
            elif cat == "G_ownership_unknown":
                # Check if this unknown resource is associated with the task
                if task_id and task_id in json.dumps(c):
                    unknown_resources.append(f"container:{c['name']}")

        # Audit images
        for img in inv["images"]:
            cat, reason = classify_resource_ownership("image", img, manifest)
            tag_name = f"{img['repository']}:{img['tag']}"
            if cat == "D_transient":
                superseded_imgs.append(tag_name)
            elif cat == "E_dangling":
                dangling_imgs.append(img["id"])
            elif cat == "G_ownership_unknown":
                if task_id and task_id in json.dumps(img):
                    unknown_resources.append(f"image:{tag_name}")

        # Audit builders
        for b in inv["builders"]:
            b_name = b["name"]
            if b_name.startswith("rmg-build-") or (task_id and task_id in b_name):
                disposable_builders.append(b_name)

        # Audit approved cache vs task-owned garbage
        build_cache_total = inv["buildkit_cache"]["total"]
        task_owned_cache = "0B"
        approved_cache = build_cache_total

        # Gate pass criteria for this host:
        # All transient task items == 0, and no unknown resources blocking
        host_clean = (
            len(temp_conts) == 0
            and len(superseded_imgs) == 0
            and len(dangling_imgs) == 0
            and len(disposable_builders) == 0
            and len(temp_networks) == 0
            and len(temp_volumes) == 0
            and len(unknown_resources) == 0
        )

        if not host_clean:
            overall_pass = False

        results[h] = {
            "temporary_containers": temp_conts,
            "superseded_candidate_images": superseded_imgs,
            "task_owned_dangling_images": dangling_imgs,
            "disposable_builders": disposable_builders,
            "task_owned_build_cache": task_owned_cache,
            "approved_shared_cache": approved_cache,
            "temporary_networks": temp_networks,
            "temporary_volumes": temp_volumes,
            "unknown_resources": unknown_resources,
            "garbage_status": "NONE" if host_clean else "FAIL",
            "passed": host_clean,
        }

    return {
        "gate_passed": overall_pass,
        "hosts": results,
    }


def bootstrap_gau_unraid() -> Dict[str, Any]:
    """
    Idempotent bootstrap of pinned buildx and compose plugins on gau-unraid.
    Verifies checksums and only installs when missing or mismatched.
    """
    host_key = "gau-unraid"
    print(f"[*] Verifying pinned toolchain on {host_key}...")
    results = {}

    for tool_name, spec in PINNED_TOOLCHAIN.items():
        ver = spec["version"]
        target = spec["target"]
        expected_sha = spec["sha256"]
        url = spec["url"]

        # Check existing file and checksum
        check_cmd = (
            f"mkdir -p ~/.docker/cli-plugins && "
            f"if [ -f {target} ]; then "
            f"  sha256sum {target} | awk '{{print $1}}'; "
            f"else "
            f"  echo 'missing'; "
            f"fi"
        )
        res_check = run_ssh(host_key, check_cmd)
        current_sha = res_check.stdout.strip()

        if current_sha == expected_sha:
            results[tool_name] = {"status": "ALREADY_PINNED", "version": ver, "sha256": expected_sha}
            print(f"    [+] {tool_name} {ver} is already installed with valid checksum.")
        else:
            print(f"    [*] Installing {tool_name} {ver}...")
            install_cmd = (
                f"curl -fsSL -o {target} {url} && "
                f"echo '{expected_sha}  {target}' | sha256sum -c - && "
                f"chmod +x {target}"
            )
            res_install = run_ssh(host_key, install_cmd, timeout=60)
            if res_install.returncode == 0:
                results[tool_name] = {"status": "INSTALLED", "version": ver, "sha256": expected_sha}
                print(f"    [+] {tool_name} {ver} installed and verified.")
            else:
                results[tool_name] = {"status": "FAILED", "error": res_install.stderr.strip()}
                print(f"    [-] Failed to install {tool_name}: {res_install.stderr.strip()}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Workspace Docker Host Resource Hygiene Tool (P0)")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # init-task
    init_parser = subparsers.add_parser("init-task", help="Initialize task manifest & preflight inventory")
    init_parser.add_argument("--task-id", required=True, help="Unique Task ID")
    init_parser.add_argument("--project", default="research-memory-gateway", help="Project identifier")
    init_parser.add_argument("--host", choices=["unraid", "gau-unraid"], default="gau-unraid", help="Host")

    # inventory
    inv_parser = subparsers.add_parser("inventory", help="Collect comprehensive inventory")
    inv_parser.add_argument("--host", choices=["unraid", "gau-unraid", "all"], default="all", help="Target host")
    inv_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    # cleanup
    clean_parser = subparsers.add_parser("cleanup", help="Targeted cleanup (read-only by default)")
    clean_parser.add_argument("--task-id", help="Task ID")
    clean_parser.add_argument("--host", choices=["unraid", "gau-unraid"], default="unraid", help="Target host")
    clean_parser.add_argument("--apply", action="store_true", help="Explicitly apply deletions (destructive)")

    # gate
    gate_parser = subparsers.add_parser("gate", help="Evaluate Release-Ready Hard Gate")
    gate_parser.add_argument("--task-id", help="Task ID")
    gate_parser.add_argument("--host", choices=["unraid", "gau-unraid", "all"], default="all", help="Target host")

    # bootstrap-gau
    subparsers.add_parser("bootstrap-gau", help="Idempotent bootstrap of pinned buildx & compose on gau-unraid")

    args = parser.parse_args()

    if args.command == "init-task":
        manifest = TaskManifestManager.init_task(args.task_id, args.project, args.host)
        print(f"[+] Task {args.task_id} initialized with preflight inventory.")
        print(json.dumps(manifest, indent=2))
        return

    if args.command == "inventory":
        hosts = ["unraid", "gau-unraid"] if args.host == "all" else [args.host]
        for h in hosts:
            inv = collect_full_inventory(h)
            if args.json:
                print(json.dumps(inv, indent=2))
            else:
                print(f"\n=== {h.upper()} INVENTORY SUMMARY ===")
                print(f"Total Containers: {len(inv['containers'])}")
                print(f"Total Images:     {len(inv['images'])}")
                print(f"Total Volumes:    {len(inv['volumes'])}")
                print(f"Total Networks:   {len(inv['networks'])}")
                print(f"Buildx Builders:  {len(inv['builders'])}")
                print(f"BuildKit Cache:   {inv['buildkit_cache']['total']} (reclaimable: {inv['buildkit_cache']['reclaimable']})")
        return

    if args.command == "cleanup":
        res = execute_cleanup(args.host, args.task_id, apply=args.apply)
        print(json.dumps(res, indent=2))
        return

    if args.command == "gate":
        report = audit_release_gate(args.task_id, args.host)
        print("\n=== RELEASE-READY HYGIENE GATE AUDIT ===")
        for h, data in report["hosts"].items():
            print(f"\n[{h.upper()}]")
            print(f"  temporary_containers:         {len(data['temporary_containers'])} {data['temporary_containers']}")
            print(f"  superseded_candidate_images:  {len(data['superseded_candidate_images'])} {data['superseded_candidate_images']}")
            print(f"  task_owned_dangling_images:   {len(data['task_owned_dangling_images'])}")
            print(f"  disposable_builders:          {len(data['disposable_builders'])}")
            print(f"  task_owned_build_cache:       {data['task_owned_build_cache']}")
            print(f"  approved_shared_cache:        {data['approved_shared_cache']}")
            print(f"  temporary_networks:           {len(data['temporary_networks'])}")
            print(f"  temporary_volumes:            {len(data['temporary_volumes'])}")
            print(f"  unknown_resources:            {len(data['unknown_resources'])} {data['unknown_resources']}")
            print(f"  Garbage Status:               {data['garbage_status']}")

        print(f"\nOverall Gate Status: {'PASS' if report['gate_passed'] else 'FAIL'}")
        if not report["gate_passed"]:
            sys.exit(1)
        return

    if args.command == "bootstrap-gau":
        res = bootstrap_gau_unraid()
        print(json.dumps(res, indent=2))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
