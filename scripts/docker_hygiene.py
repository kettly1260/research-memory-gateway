#!/usr/bin/env python3
"""
Research Memory Gateway - Workspace Generic Docker Host Resource Hygiene Tool (P0)

Fail-Closed, multi-host, manifest-driven engine for Docker host inventory,
lifecycle management, safe targeted cleanup (dry-run by default),
approved shared cache bounding, policy validation, and release-ready hard gate audits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Workspace root and policy paths
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = REPO_ROOT.parent
WORKSPACE_AGENTS_FILE = WORKSPACE_ROOT / "AGENTS.md"
CANONICAL_POLICY_FILE = REPO_ROOT / "docs" / "WORKSPACE_DOCKER_AGENT_POLICY.md"

# Base directory for task manifests
HYGIENE_BASE_DIR = REPO_ROOT / ".local" / "docker-hygiene"

# Managed markers in AGENTS.md
POLICY_BEGIN_MARKER = "<!-- BEGIN MANAGED DOCKER HYGIENE POLICY -->"
POLICY_END_MARKER = "<!-- END MANAGED DOCKER HYGIENE POLICY -->"

# Pinned toolchain versions and sha256 checksums for gau-unraid
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


class HostCollectionError(Exception):
    """Raised when an inventory or probe operation fails on a remote host (Fail-Closed)."""

    def __init__(self, host: str, command: str, returncode: int, stderr: str, reason: str):
        self.host = host
        self.command = command
        self.returncode = returncode
        self.stderr = stderr.strip()
        self.reason = reason
        super().__init__(f"[{host}] Host collection failed on '{command}': {reason} (code {returncode}) - {self.stderr}")


@dataclass
class HostConfig:
    name: str
    ssh_target: str
    is_production: bool


HOSTS: Dict[str, HostConfig] = {
    "unraid": HostConfig(name="unraid", ssh_target="unraid", is_production=True),
    "gau-unraid": HostConfig(name="gau-unraid", ssh_target="gau-unraid", is_production=False),
}


def parse_byte_size(size_str: str) -> float:
    """Parses Docker size string (e.g. '13.51GB', '500MB', '10kB', '0B') into gigabytes."""
    s = size_str.strip().upper()
    if not s or s == "0B" or s == "0":
        return 0.0
    units = {
        "GB": 1.0,
        "GIB": 1.0,
        "MB": 1.0 / 1024,
        "MIB": 1.0 / 1024,
        "KB": 1.0 / (1024 * 1024),
        "KIB": 1.0 / (1024 * 1024),
        "B": 1.0 / (1024 * 1024 * 1024),
    }
    match = re.match(r"^([\d.]+)\s*([A-Z]+)$", s)
    if match:
        val, unit = match.groups()
        return float(val) * units.get(unit, 1.0)
    return 0.0


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
    try:
        return subprocess.run(
            ssh_args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HostCollectionError(
            host=host_key,
            command=full_cmd,
            returncode=-1,
            stderr=f"SSH command timed out after {timeout} seconds",
            reason="SSH Command Timeout",
        ) from exc


def probe_host_reachability(host_key: str) -> Dict[str, Any]:
    """
    Lightweight probe checking SSH reachability and Docker daemon responsiveness.
    Must succeed before any inventory is collected.
    """
    cmd = "docker version --format '{{json .}}'"
    res = run_ssh(host_key, cmd, timeout=15)
    if res.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=cmd,
            returncode=res.returncode,
            stderr=res.stderr,
            reason="Daemon/SSH Reachability Probe Failed",
        )
    try:
        data = json.loads(res.stdout)
        server_ok = bool(data.get("Server"))
        if not server_ok:
            raise HostCollectionError(
                host=host_key,
                command=cmd,
                returncode=res.returncode,
                stderr="Docker server version not returned by daemon",
                reason="Docker Daemon Unresponsive",
            )
        return {
            "ssh_ok": True,
            "docker_ok": True,
            "server_version": data.get("Server", {}).get("Version", "unknown"),
        }
    except json.JSONDecodeError as exc:
        raise HostCollectionError(
            host=host_key,
            command=cmd,
            returncode=res.returncode,
            stderr=res.stdout,
            reason="Malformed JSON response from docker version probe",
        ) from exc


def collect_containers(host_key: str) -> List[Dict[str, Any]]:
    # Step 1: list container IDs without hiding errors
    list_cmd = "docker ps -aq"
    res_list = run_ssh(host_key, list_cmd, timeout=30)
    if res_list.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=list_cmd,
            returncode=res_list.returncode,
            stderr=res_list.stderr,
            reason="Failed to list container IDs",
        )

    ids = res_list.stdout.strip().split()
    if not ids:
        return []

    # Step 2: inspect container details
    inspect_cmd = f"docker inspect {' '.join(ids)}"
    res_insp = run_ssh(host_key, inspect_cmd, timeout=35)
    if res_insp.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=inspect_cmd,
            returncode=res_insp.returncode,
            stderr=res_insp.stderr,
            reason="Failed to inspect container details",
        )

    try:
        raw_list = json.loads(res_insp.stdout)
    except json.JSONDecodeError as exc:
        raise HostCollectionError(
            host=host_key,
            command=inspect_cmd,
            returncode=res_insp.returncode,
            stderr=res_insp.stdout[:200],
            reason="Malformed JSON in container inspect",
        ) from exc

    results = []
    for c in raw_list:
        mounts = []
        for m in c.get("Mounts", []):
            mounts.append({
                "type": m.get("Type", ""),
                "name": m.get("Name", ""),
                "source": m.get("Source", ""),
                "destination": m.get("Destination", ""),
                "rw": m.get("RW", True),
            })
        results.append({
            "host": host_key,
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
            "mounts": mounts,
            "networks": list(c.get("NetworkSettings", {}).get("Networks", {}).keys()),
        })
    return results


def collect_images(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker images --digests --no-trunc --format '{{json .}}'"
    res = run_ssh(host_key, cmd, timeout=35)
    if res.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=cmd,
            returncode=res.returncode,
            stderr=res.stderr,
            reason="Failed to list docker images",
        )

    images = []
    ids_to_inspect = []
    for line in res.stdout.strip().splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        try:
            item = json.loads(line_str)
        except json.JSONDecodeError as exc:
            raise HostCollectionError(
                host=host_key,
                command=cmd,
                returncode=res.returncode,
                stderr=line_str,
                reason="Malformed JSON line in docker images output",
            ) from exc

        img_id = item.get("ID", "")
        images.append({
            "host": host_key,
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

    # Batch inspect all image labels in chunks of 25 (100% coverage, fail-closed)
    batch_size = 25
    label_map: Dict[str, Dict[str, str]] = {}
    for i in range(0, len(ids_to_inspect), batch_size):
        chunk = ids_to_inspect[i:i + batch_size]
        inspect_cmd = f"docker image inspect {' '.join(chunk)}"
        res_insp = run_ssh(host_key, inspect_cmd, timeout=35)
        if res_insp.returncode != 0:
            raise HostCollectionError(
                host=host_key,
                command=inspect_cmd,
                returncode=res_insp.returncode,
                stderr=res_insp.stderr,
                reason=f"Failed to inspect image batch {i // batch_size + 1}",
            )
        try:
            insp_list = json.loads(res_insp.stdout)
            for item in insp_list:
                full_id = item.get("Id", "")
                lbls = item.get("Config", {}).get("Labels") or {}
                label_map[full_id] = lbls
        except json.JSONDecodeError as exc:
            raise HostCollectionError(
                host=host_key,
                command=inspect_cmd,
                returncode=res_insp.returncode,
                stderr=res_insp.stdout[:200],
                reason="Malformed JSON in image inspect batch",
            ) from exc

    for img in images:
        img["labels"] = label_map.get(img["full_id"], {})

    return images


def collect_volumes(host_key: str) -> List[Dict[str, Any]]:
    list_cmd = "docker volume ls -q"
    res_list = run_ssh(host_key, list_cmd, timeout=30)
    if res_list.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=list_cmd,
            returncode=res_list.returncode,
            stderr=res_list.stderr,
            reason="Failed to list docker volumes",
        )

    names = res_list.stdout.strip().split()
    if not names:
        return []

    inspect_cmd = f"docker volume inspect {' '.join(names)}"
    res_insp = run_ssh(host_key, inspect_cmd, timeout=30)
    if res_insp.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=inspect_cmd,
            returncode=res_insp.returncode,
            stderr=res_insp.stderr,
            reason="Failed to inspect docker volumes",
        )
    try:
        raw_list = json.loads(res_insp.stdout)
    except json.JSONDecodeError as exc:
        raise HostCollectionError(
            host=host_key,
            command=inspect_cmd,
            returncode=res_insp.returncode,
            stderr=res_insp.stdout[:200],
            reason="Malformed JSON in volume inspect",
        ) from exc

    results = []
    for v in raw_list:
        results.append({
            "host": host_key,
            "name": v.get("Name", ""),
            "driver": v.get("Driver", ""),
            "mountpoint": v.get("Mountpoint", ""),
            "labels": v.get("Labels") or {},
        })
    return results


def collect_networks(host_key: str) -> List[Dict[str, Any]]:
    list_cmd = "docker network ls -q"
    res_list = run_ssh(host_key, list_cmd, timeout=30)
    if res_list.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=list_cmd,
            returncode=res_list.returncode,
            stderr=res_list.stderr,
            reason="Failed to list docker networks",
        )

    ids = res_list.stdout.strip().split()
    if not ids:
        return []

    inspect_cmd = f"docker network inspect {' '.join(ids)}"
    res_insp = run_ssh(host_key, inspect_cmd, timeout=30)
    if res_insp.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=inspect_cmd,
            returncode=res_insp.returncode,
            stderr=res_insp.stderr,
            reason="Failed to inspect docker networks",
        )
    try:
        raw_list = json.loads(res_insp.stdout)
    except json.JSONDecodeError as exc:
        raise HostCollectionError(
            host=host_key,
            command=inspect_cmd,
            returncode=res_insp.returncode,
            stderr=res_insp.stdout[:200],
            reason="Malformed JSON in network inspect",
        ) from exc

    results = []
    for n in raw_list:
        results.append({
            "host": host_key,
            "id": n.get("Id", "")[:12],
            "full_id": n.get("Id", ""),
            "name": n.get("Name", ""),
            "driver": n.get("Driver", ""),
            "scope": n.get("Scope", ""),
            "labels": n.get("Labels") or {},
            "containers": list(n.get("Containers", {}).keys()),
        })
    return results


def collect_buildx_builders(host_key: str) -> List[Dict[str, Any]]:
    cmd = "docker buildx ls"
    res = run_ssh(host_key, cmd, timeout=20)
    if res.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=cmd,
            returncode=res.returncode,
            stderr=res.stderr,
            reason="Failed to list docker buildx builders",
        )

    builders = []
    for line in res.stdout.strip().splitlines():
        line_str = line.strip()
        if not line_str or line_str.startswith("NAME") or line_str.startswith("\\_"):
            continue
        parts = line_str.split()
        if parts:
            builder_name = parts[0].rstrip("*")
            builders.append({
                "host": host_key,
                "name": builder_name,
                "is_current": "*" in parts[0],
                "raw": line_str,
            })
    return builders


def collect_buildkit_cache(host_key: str) -> Dict[str, Any]:
    cmd = "docker buildx du"
    res = run_ssh(host_key, cmd, timeout=25)
    if res.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=cmd,
            returncode=res.returncode,
            stderr=res.stderr,
            reason="Failed to query buildx du cache",
        )

    reclaimable = "0B"
    total = "0B"
    entries_count = 0
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
    cmd = "docker system df --format '{{json .}}'"
    res = run_ssh(host_key, cmd, timeout=25)
    if res.returncode != 0:
        raise HostCollectionError(
            host=host_key,
            command=cmd,
            returncode=res.returncode,
            stderr=res.stderr,
            reason="Failed to query docker system df",
        )

    items = []
    for line in res.stdout.strip().splitlines():
        line_str = line.strip()
        if not line_str:
            continue
        try:
            items.append(json.loads(line_str))
        except json.JSONDecodeError as exc:
            raise HostCollectionError(
                host=host_key,
                command=cmd,
                returncode=res.returncode,
                stderr=line_str,
                reason="Malformed JSON in docker system df",
            ) from exc
    return items


def collect_full_inventory(host_key: str) -> Dict[str, Any]:
    """
    Collects comprehensive inventory across all Docker object types (Fail-Closed).
    Runs reachability probe first; raises HostCollectionError if any step fails.
    """
    probe = probe_host_reachability(host_key)
    containers = collect_containers(host_key)
    images = collect_images(host_key)
    volumes = collect_volumes(host_key)
    networks = collect_networks(host_key)
    builders = collect_buildx_builders(host_key)
    cache = collect_buildkit_cache(host_key)
    df = collect_system_df(host_key)

    referenced_image_ids = {c["image_full_id"] for c in containers if c.get("image_full_id")}
    referenced_network_names = {net for c in containers for net in c.get("networks", [])}
    referenced_named_volumes = {
        m["name"] for c in containers for m in c.get("mounts", [])
        if m.get("type") == "volume" and m.get("name")
    }

    return {
        "host": host_key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "probe": probe,
        "observation_complete": True,
        "containers": containers,
        "images": images,
        "volumes": volumes,
        "networks": networks,
        "builders": builders,
        "buildkit_cache": cache,
        "system_df": df,
        "referenced_image_ids": list(referenced_image_ids),
        "referenced_network_names": list(referenced_network_names),
        "referenced_named_volumes": list(referenced_named_volumes),
    }


class TaskManifestManager:
    """Manages multi-host task manifests, preflight, and postflight delta state."""

    @staticmethod
    def get_task_dir(task_id: str) -> Path:
        p = HYGIENE_BASE_DIR / task_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    @classmethod
    def get_host_dir(cls, task_id: str, host: str) -> Path:
        h_dir = cls.get_task_dir(task_id) / "hosts" / host
        h_dir.mkdir(parents=True, exist_ok=True)
        return h_dir

    @classmethod
    def load_manifest(cls, task_id: str) -> Optional[Dict[str, Any]]:
        manifest_file = cls.get_task_dir(task_id) / "manifest.json"
        if manifest_file.exists():
            with open(manifest_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    @classmethod
    def save_manifest(cls, manifest: Dict[str, Any]) -> None:
        task_id = manifest["task_id"]
        manifest_file = cls.get_task_dir(task_id) / "manifest.json"
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    @classmethod
    def load_preflight(cls, task_id: str, host: str) -> Optional[Dict[str, Any]]:
        preflight_file = cls.get_host_dir(task_id, host) / "preflight.json"
        if preflight_file.exists():
            with open(preflight_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    @classmethod
    def init_task_host(cls, task_id: str, project: str, host: str, cache_policy: str = "default") -> Dict[str, Any]:
        """
        Initializes preflight inventory for a specific host under a task (Fail-Closed).
        """
        manifest = cls.load_manifest(task_id)
        if not manifest:
            manifest = {
                "task_id": task_id,
                "project": project,
                "start_time": datetime.now(timezone.utc).isoformat(),
                "finish_time": None,
                "hosts": {},
            }

        host_dir = cls.get_host_dir(task_id, host)
        preflight_data = collect_full_inventory(host)
        with open(host_dir / "preflight.json", "w", encoding="utf-8") as f:
            json.dump(preflight_data, f, indent=2, ensure_ascii=False)

        manifest["hosts"][host] = {
            "status": "started",
            "preflight_time": preflight_data["timestamp"],
            "postflight_time": None,
            "cache_policy": cache_policy,
            "active_candidates": [],
            "retained_resources": [],
            "cleanup_plan": None,
            "cleanup_result": None,
            "gate_report": None,
        }
        cls.save_manifest(manifest)
        return manifest

    @classmethod
    def record_postflight(cls, task_id: str, host: str) -> Dict[str, Any]:
        host_dir = cls.get_host_dir(task_id, host)
        postflight_data = collect_full_inventory(host)
        with open(host_dir / "postflight.json", "w", encoding="utf-8") as f:
            json.dump(postflight_data, f, indent=2, ensure_ascii=False)
        return postflight_data


def compute_inventory_delta(
    preflight: Dict[str, Any],
    current: Dict[str, Any],
) -> Dict[str, List[Any]]:
    """
    Computes new resources created between preflight and current inventory using stable IDs.
    """
    pre_cont_ids = {c["full_id"] for c in preflight.get("containers", [])}
    pre_cont_names = {c["name"] for c in preflight.get("containers", [])}
    new_containers = [
        c for c in current.get("containers", [])
        if c["full_id"] not in pre_cont_ids and c["name"] not in pre_cont_names
    ]

    pre_img_ids = {img["full_id"] for img in preflight.get("images", [])}
    new_images = [
        img for img in current.get("images", [])
        if img["full_id"] not in pre_img_ids
    ]

    pre_vol_names = {v["name"] for v in preflight.get("volumes", [])}
    new_volumes = [
        v for v in current.get("volumes", [])
        if v["name"] not in pre_vol_names
    ]

    pre_net_ids = {n.get("full_id", n.get("id")) for n in preflight.get("networks", [])}
    pre_net_names = {n["name"] for n in preflight.get("networks", [])}
    new_networks = [
        n for n in current.get("networks", [])
        if n.get("full_id", n.get("id")) not in pre_net_ids and n["name"] not in pre_net_names
    ]

    pre_builder_names = {b["name"] for b in preflight.get("builders", [])}
    new_builders = [
        b for b in current.get("builders", [])
        if b["name"] not in pre_builder_names
    ]

    return {
        "new_containers": new_containers,
        "new_images": new_images,
        "new_volumes": new_volumes,
        "new_networks": new_networks,
        "new_builders": new_builders,
    }


def classify_resource_ownership(
    res_type: str,
    res: Dict[str, Any],
    task_id: Optional[str] = None,
    manifest: Optional[Dict[str, Any]] = None,
    is_delta: bool = False,
    host_key: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Determines resource category (A~G) and rationale.
    Accepts explicit host_key context for accurate candidate resolution.
    """
    labels = res.get("labels") or {}
    lifecycle = labels.get("io.agent.lifecycle") or labels.get("io.rmg.lifecycle")
    res_task = labels.get("io.agent.task") or labels.get("io.rmg.task")
    name = res.get("name", "")
    repo = res.get("repository", "")
    tag = res.get("tag", "")
    image = res.get("image", "")
    effective_host = host_key or res.get("host", "")

    # Universal lifecycle labels
    if lifecycle == "production":
        return "A_production", "Explicit lifecycle=production label"
    if lifecycle == "rollback":
        return "B_rollback", "Explicit lifecycle=rollback label"
    if lifecycle == "candidate":
        # Candidate supersession resolution using manifest active_candidates
        if manifest and task_id:
            host_info = manifest.get("hosts", {}).get(effective_host, {})
            active_cands = host_info.get("active_candidates", [])
            tag_full = f"{repo}:{tag}"
            if tag_full in active_cands or (tag in active_cands):
                return "C_candidate", "Active candidate registered in manifest"
            if len(active_cands) > 0 and (tag_full not in active_cands and tag not in active_cands):
                return "D_transient", "Candidate superseded by active candidate"
        return "C_candidate", "Candidate lifecycle label"
    if lifecycle == "temporary":
        return "D_transient", "Explicit lifecycle=temporary label"

    # Match task ID directly
    if task_id and (res_task == task_id or (f"agent-{task_id}" in name)):
        return "D_transient", f"Belongs to task {task_id}"

    # Known production & rollback baseline signatures on Unraid
    if "research-memory-gateway" in name or "research-memory-gateway" in repo or "research-memory-gateway" in image:
        if name == "research-memory-gateway" and res.get("running"):
            return "A_production", "Live running production container"
        if tag in ("v0.2.5.1-local",):
            return "A_production", "Active production image"
        if tag in ("v0.2.5", "latest"):
            return "B_rollback", "Designated rollback image"

    # Other recognized external project indicators
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

    # Default docker network & builder primitives
    if res_type == "network" and name in ("bridge", "host", "none"):
        return "F_other_projects", "Default Docker system network"
    if res_type == "builder" and name in ("default", "rootless"):
        return "F_other_projects", "Default host Docker builder"

    # If it's a delta resource created during this task and still unassigned:
    if is_delta:
        return "G_new_unknown", "Newly created resource without verified ownership signature"

    # If it already existed in preflight:
    return "G_preexisting_unknown", "Pre-existing resource with unknown ownership"


def plan_task_cleanup(
    host_key: str,
    task_id: str,
) -> Dict[str, Any]:
    """
    Builds a targeted cleanup plan for a specific task based on preflight delta.
    Validates active container references on volumes and networks before planning deletion.
    """
    manifest = TaskManifestManager.load_manifest(task_id)
    preflight = TaskManifestManager.load_preflight(task_id, host_key)
    current = collect_full_inventory(host_key)

    if not preflight:
        return {
            "error": f"No preflight inventory found for task {task_id} on host {host_key}",
            "containers_to_delete": [],
            "images_to_delete": [],
            "volumes_to_delete": [],
            "networks_to_delete": [],
            "builders_to_delete": [],
        }

    delta = compute_inventory_delta(preflight, current)

    containers_to_delete = []
    images_to_delete = []
    volumes_to_delete = []
    networks_to_delete = []
    builders_to_delete = []
    protected_skipped = []
    new_unknown_resources = []
    preexisting_unknown_resources = []

    # Running / protected container reference maps
    referenced_named_vols: Set[str] = set()
    referenced_net_names: Set[str] = set()

    for c in current["containers"]:
        cat, _ = classify_resource_ownership("container", c, task_id, manifest, is_delta=False, host_key=host_key)
        # If container is running or protected (not in containers_to_delete):
        if c.get("running") or cat in ("A_production", "B_rollback", "F_other_projects"):
            for m in c.get("mounts", []):
                if m.get("type") == "volume" and m.get("name"):
                    referenced_named_vols.add(m["name"])
            for net in c.get("networks", []):
                referenced_net_names.add(net)

    # 1. Delta containers
    for c in delta["new_containers"]:
        cat, reason = classify_resource_ownership("container", c, task_id, manifest, is_delta=True, host_key=host_key)
        if cat == "D_transient":
            containers_to_delete.append({"id": c["id"], "name": c["name"], "reason": reason})
        elif cat == "G_new_unknown":
            new_unknown_resources.append({"type": "container", "id": c["id"], "name": c["name"], "reason": reason})
        else:
            protected_skipped.append({"id": c["id"], "name": c["name"], "category": cat, "reason": reason})

    # 2. Delta images
    for img in delta["new_images"]:
        cat, reason = classify_resource_ownership("image", img, task_id, manifest, is_delta=True, host_key=host_key)
        tag_str = f"{img['repository']}:{img['tag']}"
        if cat in ("D_transient", "E_dangling"):
            images_to_delete.append({"id": img["id"], "tag": tag_str, "reason": reason})
        elif cat == "G_new_unknown":
            new_unknown_resources.append({"type": "image", "id": img["id"], "tag": tag_str, "reason": reason})
        else:
            protected_skipped.append({"id": img["id"], "tag": tag_str, "category": cat, "reason": reason})

    # 3. Delta networks
    for net in delta["new_networks"]:
        cat, reason = classify_resource_ownership("network", net, task_id, manifest, is_delta=True, host_key=host_key)
        if cat == "D_transient":
            if net["name"] in referenced_net_names:
                protected_skipped.append({"id": net["id"], "name": net["name"], "reason": "Referenced by running/protected container"})
            else:
                networks_to_delete.append({"id": net["id"], "name": net["name"], "reason": reason})
        elif cat == "G_new_unknown":
            new_unknown_resources.append({"type": "network", "id": net["id"], "name": net["name"], "reason": reason})
        else:
            protected_skipped.append({"id": net["id"], "name": net["name"], "category": cat, "reason": reason})

    # 4. Delta volumes
    for vol in delta["new_volumes"]:
        cat, reason = classify_resource_ownership("volume", vol, task_id, manifest, is_delta=True, host_key=host_key)
        if cat == "D_transient":
            if vol["name"] in referenced_named_vols:
                protected_skipped.append({"name": vol["name"], "reason": "Referenced by running/protected container (Fail-Closed)"})
            else:
                volumes_to_delete.append({"name": vol["name"], "reason": reason})
        elif cat == "G_new_unknown":
            new_unknown_resources.append({"type": "volume", "name": vol["name"], "reason": reason})
        else:
            protected_skipped.append({"name": vol["name"], "category": cat, "reason": reason})

    # 5. Delta builders
    for bld in delta["new_builders"]:
        cat, reason = classify_resource_ownership("builder", bld, task_id, manifest, is_delta=True, host_key=host_key)
        if cat == "D_transient" or bld["name"].startswith(f"agent-{task_id}"):
            builders_to_delete.append({"name": bld["name"], "reason": reason})
        elif cat == "G_new_unknown":
            new_unknown_resources.append({"type": "builder", "name": bld["name"], "reason": reason})

    # Pre-existing unknown count
    for c in current["containers"]:
        if c["full_id"] not in {dc["full_id"] for dc in delta["new_containers"]}:
            cat, _ = classify_resource_ownership("container", c, task_id, manifest, is_delta=False, host_key=host_key)
            if cat == "G_preexisting_unknown":
                preexisting_unknown_resources.append({"type": "container", "id": c["id"], "name": c["name"]})

    for img in current["images"]:
        if img["full_id"] not in {di["full_id"] for di in delta["new_images"]}:
            cat, _ = classify_resource_ownership("image", img, task_id, manifest, is_delta=False, host_key=host_key)
            if cat == "G_preexisting_unknown":
                preexisting_unknown_resources.append({"type": "image", "id": img["id"], "tag": f"{img['repository']}:{img['tag']}"})

    return {
        "host": host_key,
        "task_id": task_id,
        "dry_run": True,
        "containers_to_delete": containers_to_delete,
        "images_to_delete": images_to_delete,
        "networks_to_delete": networks_to_delete,
        "volumes_to_delete": volumes_to_delete,
        "builders_to_delete": builders_to_delete,
        "protected_skipped": protected_skipped,
        "new_unknown_resources": new_unknown_resources,
        "preexisting_unknown_count": len(preexisting_unknown_resources),
    }


def execute_task_cleanup(
    host_key: str,
    task_id: str,
    apply: bool = False,
) -> Dict[str, Any]:
    """
    Executes task cleanup plan with fail-closed semantics:
    - DRY-RUN by default unless apply=True.
    - Requires valid task_id and preflight.
    - Fails if any deletion returns non-zero.
    """
    plan = plan_task_cleanup(host_key, task_id)
    if "error" in plan:
        return plan

    plan["dry_run"] = not apply
    if not apply:
        print(f"[!] DRY-RUN MODE ({host_key}): No resources will be deleted. Pass --apply to execute.")
        return plan

    executed_actions = []
    all_succeeded = True

    # 1. Containers: stop and remove
    for c in plan["containers_to_delete"]:
        c_id = c["id"]
        res = run_ssh(host_key, f"docker rm -f {c_id}")
        success = (res.returncode == 0)
        if not success:
            all_succeeded = False
        executed_actions.append({
            "target": c_id,
            "action": "docker rm -f",
            "success": success,
            "output": res.stdout.strip() if success else res.stderr.strip(),
        })

    # 2. Networks: remove
    for net in plan["networks_to_delete"]:
        net_id = net["id"]
        res = run_ssh(host_key, f"docker network rm {net_id}")
        success = (res.returncode == 0)
        if not success:
            all_succeeded = False
        executed_actions.append({
            "target": net_id,
            "action": "docker network rm",
            "success": success,
            "output": res.stdout.strip() if success else res.stderr.strip(),
        })

    # 3. Volumes: remove
    for vol in plan["volumes_to_delete"]:
        v_name = vol["name"]
        res = run_ssh(host_key, f"docker volume rm {v_name}")
        success = (res.returncode == 0)
        if not success:
            all_succeeded = False
        executed_actions.append({
            "target": v_name,
            "action": "docker volume rm",
            "success": success,
            "output": res.stdout.strip() if success else res.stderr.strip(),
        })

    # 4. Images: remove
    for img in plan["images_to_delete"]:
        img_id = img["id"]
        res = run_ssh(host_key, f"docker rmi {img_id}")
        success = (res.returncode == 0)
        if not success:
            all_succeeded = False
        executed_actions.append({
            "target": img_id,
            "action": "docker rmi",
            "success": success,
            "output": res.stdout.strip() if success else res.stderr.strip(),
        })

    # 5. Builders: remove
    for bld in plan["builders_to_delete"]:
        b_name = bld["name"]
        res = run_ssh(host_key, f"docker buildx rm {b_name}")
        success = (res.returncode == 0)
        if not success:
            all_succeeded = False
        executed_actions.append({
            "target": b_name,
            "action": "docker buildx rm",
            "success": success,
            "output": res.stdout.strip() if success else res.stderr.strip(),
        })

    plan["cleanup_success"] = all_succeeded
    plan["executed_actions"] = executed_actions
    return plan


def audit_task_completion_gate(task_id: str) -> Dict[str, Any]:
    """
    Task Completion Gate: evaluates whether THIS task left transient garbage,
    unapproved cache deltas, or new_unknowns.
    """
    manifest = TaskManifestManager.load_manifest(task_id)
    if not manifest:
        return {
            "gate_passed": False,
            "error": f"Task manifest not found: {task_id}",
            "hosts": {},
        }

    overall_pass = True
    host_reports = {}

    for host_key, host_data in manifest.get("hosts", {}).items():
        preflight = TaskManifestManager.load_preflight(task_id, host_key)
        current = collect_full_inventory(host_key)

        if not preflight:
            overall_pass = False
            host_reports[host_key] = {"error": "Missing preflight inventory", "gate_passed": False}
            continue

        delta = compute_inventory_delta(preflight, current)

        temp_conts = []
        superseded_imgs = []
        dangling_imgs = []
        disposable_builders = []
        temp_networks = []
        temp_volumes = []
        new_unknown = []
        preexisting_unknown_count = 0

        # Delta containers
        for c in delta["new_containers"]:
            cat, reason = classify_resource_ownership("container", c, task_id, manifest, is_delta=True, host_key=host_key)
            if cat == "D_transient":
                temp_conts.append(c["name"])
            elif cat == "G_new_unknown":
                new_unknown.append(f"container:{c['name']}")

        # Delta images
        for img in delta["new_images"]:
            cat, reason = classify_resource_ownership("image", img, task_id, manifest, is_delta=True, host_key=host_key)
            tag_name = f"{img['repository']}:{img['tag']}"
            if cat == "D_transient":
                superseded_imgs.append(tag_name)
            elif cat == "E_dangling":
                dangling_imgs.append(img["id"])
            elif cat == "G_new_unknown":
                new_unknown.append(f"image:{tag_name}")

        # Delta networks
        for net in delta["new_networks"]:
            cat, reason = classify_resource_ownership("network", net, task_id, manifest, is_delta=True, host_key=host_key)
            if cat == "D_transient":
                temp_networks.append(net["name"])
            elif cat == "G_new_unknown":
                new_unknown.append(f"network:{net['name']}")

        # Delta volumes
        for vol in delta["new_volumes"]:
            cat, reason = classify_resource_ownership("volume", vol, task_id, manifest, is_delta=True, host_key=host_key)
            if cat == "D_transient":
                temp_volumes.append(vol["name"])
            elif cat == "G_new_unknown":
                new_unknown.append(f"volume:{vol['name']}")

        # Delta builders
        for bld in delta["new_builders"]:
            cat, reason = classify_resource_ownership("builder", bld, task_id, manifest, is_delta=True, host_key=host_key)
            if cat == "D_transient" or bld["name"].startswith(f"agent-{task_id}"):
                disposable_builders.append(bld["name"])
            elif cat == "G_new_unknown":
                new_unknown.append(f"builder:{bld['name']}")

        # Count pre-existing unknown resources
        for c in current["containers"]:
            if c["full_id"] not in {dc["full_id"] for dc in delta["new_containers"]}:
                cat, _ = classify_resource_ownership("container", c, task_id, manifest, is_delta=False, host_key=host_key)
                if cat == "G_preexisting_unknown":
                    preexisting_unknown_count += 1
        for img in current["images"]:
            if img["full_id"] not in {di["full_id"] for di in delta["new_images"]}:
                cat, _ = classify_resource_ownership("image", img, task_id, manifest, is_delta=False, host_key=host_key)
                if cat == "G_preexisting_unknown":
                    preexisting_unknown_count += 1

        # Cache calculation: compare preflight and postflight build cache
        pre_cache_total = preflight.get("buildkit_cache", {}).get("total", "0B")
        cur_cache_total = current.get("buildkit_cache", {}).get("total", "0B")
        pre_gb = parse_byte_size(pre_cache_total)
        cur_gb = parse_byte_size(cur_cache_total)
        cache_delta_gb = max(0.0, cur_gb - pre_gb)

        cache_policy = host_data.get("cache_policy", "default")
        approved_cache_delta = 0.0
        unattributed_cache_delta = 0.0

        if cache_policy == "approved_shared":
            approved_cache_delta = cache_delta_gb
        else:
            unattributed_cache_delta = cache_delta_gb

        # Evaluate gau-unraid cache limits
        cache_warning = None
        if host_key == "gau-unraid":
            if cur_gb > GAU_CACHE_LIMITS["hard_limit_gb"]:
                overall_pass = False
                cache_warning = f"EXCEEDED HARD LIMIT ({cur_gb:.2f}GB > {GAU_CACHE_LIMITS['hard_limit_gb']}GB)"
            elif cur_gb > GAU_CACHE_LIMITS["soft_limit_gb"]:
                cache_warning = f"WARNING: Exceeded soft limit ({cur_gb:.2f}GB > {GAU_CACHE_LIMITS['soft_limit_gb']}GB)"

        # Task clean assertion: all transient == 0, new_unknown == 0, and unattributed cache delta == 0
        host_clean = (
            len(temp_conts) == 0
            and len(superseded_imgs) == 0
            and len(dangling_imgs) == 0
            and len(disposable_builders) == 0
            and len(temp_networks) == 0
            and len(temp_volumes) == 0
            and len(new_unknown) == 0
            and unattributed_cache_delta == 0.0
            and (cache_warning is None or "EXCEEDED HARD LIMIT" not in cache_warning)
        )

        if not host_clean:
            overall_pass = False

        host_reports[host_key] = {
            "temporary_containers": temp_conts,
            "superseded_candidate_images": superseded_imgs,
            "task_owned_dangling_images": dangling_imgs,
            "disposable_builders": disposable_builders,
            "temporary_networks": temp_networks,
            "temporary_volumes": temp_volumes,
            "approved_shared_cache_delta": f"{approved_cache_delta:.2f}GB",
            "unattributed_cache_delta": f"{unattributed_cache_delta:.2f}GB",
            "host_build_cache_total": cur_cache_total,
            "cache_warning": cache_warning,
            "preexisting_unknown_resources": preexisting_unknown_count,
            "new_unknown_resources": new_unknown,
            "garbage_status": "NONE" if host_clean else "FAIL",
            "passed": host_clean,
        }

    return {
        "task_id": task_id,
        "gate_passed": overall_pass,
        "hosts": host_reports,
    }


def audit_host_posture(host_key: str) -> Dict[str, Any]:
    """
    Host Hygiene Audit (read-only posture report):
    Audits overall host posture. Raises HostCollectionError if unreachable.
    """
    inv = collect_full_inventory(host_key)
    production_items = []
    rollback_items = []
    candidates = []
    other_projects = []
    unknowns = []

    for c in inv["containers"]:
        cat, reason = classify_resource_ownership("container", c, is_delta=False, host_key=host_key)
        item = {"id": c["id"], "name": c["name"], "image": c["image"], "reason": reason}
        if cat == "A_production":
            production_items.append(item)
        elif cat == "B_rollback":
            rollback_items.append(item)
        elif cat == "C_candidate":
            candidates.append(item)
        elif cat == "F_other_projects":
            other_projects.append(item)
        else:
            unknowns.append(item)

    for img in inv["images"]:
        cat, reason = classify_resource_ownership("image", img, is_delta=False, host_key=host_key)
        tag_str = f"{img['repository']}:{img['tag']}"
        item = {"id": img["id"], "tag": tag_str, "size": img["size"], "reason": reason}
        if cat == "A_production":
            production_items.append(item)
        elif cat == "B_rollback":
            rollback_items.append(item)
        elif cat == "C_candidate":
            candidates.append(item)
        elif cat == "F_other_projects":
            other_projects.append(item)
        else:
            unknowns.append(item)

    return {
        "host": host_key,
        "timestamp": inv["timestamp"],
        "observation_status": "COMPLETE",
        "total_containers": len(inv["containers"]),
        "total_images": len(inv["images"]),
        "total_volumes": len(inv["volumes"]),
        "total_networks": len(inv["networks"]),
        "production_resources": production_items,
        "rollback_resources": rollback_items,
        "active_candidates": candidates,
        "other_projects_count": len(other_projects),
        "preexisting_unknown_count": len(unknowns),
        "buildkit_cache": inv["buildkit_cache"],
    }


def verify_policy_sync() -> bool:
    """Checks if WORKSPACE_AGENTS_FILE managed block matches CANONICAL_POLICY_FILE."""
    if not CANONICAL_POLICY_FILE.exists():
        print(f"[-] Canonical policy missing: {CANONICAL_POLICY_FILE}")
        return False
    if not WORKSPACE_AGENTS_FILE.exists():
        print(f"[-] Workspace AGENTS.md missing: {WORKSPACE_AGENTS_FILE}")
        return False

    with open(CANONICAL_POLICY_FILE, "r", encoding="utf-8") as f:
        canonical_text = f.read().strip()

    with open(WORKSPACE_AGENTS_FILE, "r", encoding="utf-8") as f:
        agents_text = f.read()

    pattern = re.compile(
        re.escape(POLICY_BEGIN_MARKER) + r"(.*?)" + re.escape(POLICY_END_MARKER),
        re.DOTALL,
    )
    match = pattern.search(agents_text)
    if not match:
        print(f"[-] Managed markers not found in {WORKSPACE_AGENTS_FILE}")
        return False

    managed_content = match.group(1).strip()
    return managed_content == canonical_text


def install_policy_to_workspace() -> bool:
    """Idempotently installs the canonical policy into WORKSPACE_AGENTS_FILE between markers."""
    if not CANONICAL_POLICY_FILE.exists():
        print(f"[-] Canonical policy missing: {CANONICAL_POLICY_FILE}")
        return False

    with open(CANONICAL_POLICY_FILE, "r", encoding="utf-8") as f:
        canonical_text = f.read().strip()

    agents_content = ""
    if WORKSPACE_AGENTS_FILE.exists():
        with open(WORKSPACE_AGENTS_FILE, "r", encoding="utf-8") as f:
            agents_content = f.read()

    managed_block = f"{POLICY_BEGIN_MARKER}\n{canonical_text}\n{POLICY_END_MARKER}"

    pattern = re.compile(
        re.escape(POLICY_BEGIN_MARKER) + r"(.*?)" + re.escape(POLICY_END_MARKER),
        re.DOTALL,
    )
    if pattern.search(agents_content):
        updated_content = pattern.sub(managed_block, agents_content)
    else:
        updated_content = agents_content.rstrip() + "\n\n---\n\n" + managed_block + "\n"

    with open(WORKSPACE_AGENTS_FILE, "w", encoding="utf-8") as f:
        f.write(updated_content)

    print(f"[+] Installed canonical policy into {WORKSPACE_AGENTS_FILE}")
    return True


def bootstrap_gau_unraid() -> Dict[str, Any]:
    """Idempotent bootstrap of pinned buildx & compose plugins on gau-unraid."""
    host_key = "gau-unraid"
    print(f"[*] Verifying pinned toolchain on {host_key}...")
    results = {}

    for tool_name, spec in PINNED_TOOLCHAIN.items():
        ver = spec["version"]
        target = spec["target"]
        expected_sha = spec["sha256"]
        url = spec["url"]

        check_cmd = (
            f"mkdir -p ~/.docker/cli-plugins && "
            f"if [ -f {target} ]; then "
            f"  sha256sum {target} | awk '{{print $1}}'; "
            f"else "
            f"  echo 'missing'; "
            f"fi"
        )
        res_check = run_ssh(host_key, check_cmd)
        if res_check.returncode != 0:
            raise HostCollectionError(
                host=host_key,
                command=check_cmd,
                returncode=res_check.returncode,
                stderr=res_check.stderr,
                reason="Failed to check plugin status",
            )
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
                raise HostCollectionError(
                    host=host_key,
                    command=install_cmd,
                    returncode=res_install.returncode,
                    stderr=res_install.stderr,
                    reason=f"Failed to bootstrap {tool_name}",
                )

    return results


def main():
    parser = argparse.ArgumentParser(description="Workspace Docker Host Resource Hygiene Tool (P0)")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # task-start
    start_parser = subparsers.add_parser("task-start", help="Initialize task manifest & host preflight")
    start_parser.add_argument("--task-id", required=True, help="Unique Task ID")
    start_parser.add_argument("--project", default="research-memory-gateway", help="Project name")
    start_parser.add_argument("--host", choices=["unraid", "gau-unraid"], required=True, help="Host to initialize")
    start_parser.add_argument("--cache-policy", choices=["default", "approved_shared"], default="default", help="Build cache policy")

    # task-status
    status_parser = subparsers.add_parser("task-status", help="Inspect task status and deltas")
    status_parser.add_argument("--task-id", required=True, help="Task ID")

    # task-finish
    finish_parser = subparsers.add_parser("task-finish", help="Finish task: postflight, cleanup, gate")
    finish_parser.add_argument("--task-id", required=True, help="Task ID")
    finish_parser.add_argument("--cleanup", action="store_true", help="Execute cleanup phase")
    finish_parser.add_argument("--apply", action="store_true", help="Apply cleanup deletions (destructive)")

    # audit-host
    audit_parser = subparsers.add_parser("audit-host", help="Read-only host posture audit")
    audit_parser.add_argument("--host", choices=["unraid", "gau-unraid", "all"], default="all", help="Target host")
    audit_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    # policy-check & policy-install
    subparsers.add_parser("policy-check", help="Verify workspace AGENTS.md policy sync")
    subparsers.add_parser("policy-install", help="Install canonical policy to workspace AGENTS.md")

    # bootstrap-gau
    subparsers.add_parser("bootstrap-gau", help="Bootstrap pinned toolchain on gau-unraid")

    args = parser.parse_args()

    if args.command == "task-start":
        try:
            manifest = TaskManifestManager.init_task_host(args.task_id, args.project, args.host, args.cache_policy)
            print(f"[+] Task {args.task_id} initialized on {args.host} with preflight inventory.")
            print(json.dumps(manifest, indent=2))
        except HostCollectionError as exc:
            print(f"[-] FATAL (Fail-Closed): {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "task-status":
        manifest = TaskManifestManager.load_manifest(args.task_id)
        if not manifest:
            print(f"[-] Task manifest not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)
        print(f"\n=== TASK STATUS: {args.task_id} ===")
        print(f"Project: {manifest.get('project')}, Start Time: {manifest.get('start_time')}")
        status_ok = True
        for h, info in manifest.get("hosts", {}).items():
            pre = TaskManifestManager.load_preflight(args.task_id, h)
            try:
                cur = collect_full_inventory(h)
                delta = compute_inventory_delta(pre or {}, cur)
                print(f"\n[{h.upper()}] Status: {info.get('status')}")
                print(f"  New Containers: {[c['name'] for c in delta['new_containers']]}")
                new_img_tags = [f"{i['repository']}:{i['tag']}" for i in delta['new_images']]
                print(f"  New Images:     {new_img_tags}")
                print(f"  New Networks:   {[n['name'] for n in delta['new_networks']]}")
                print(f"  New Volumes:    {[v['name'] for v in delta['new_volumes']]}")
                print(f"  New Builders:   {[b['name'] for b in delta['new_builders']]}")
            except HostCollectionError as exc:
                print(f"\n[{h.upper()}] Status: INDETERMINATE - {exc}", file=sys.stderr)
                status_ok = False
        if not status_ok:
            sys.exit(1)
        return

    if args.command == "task-finish":
        manifest = TaskManifestManager.load_manifest(args.task_id)
        if not manifest:
            print(f"[-] Task manifest not found: {args.task_id}", file=sys.stderr)
            sys.exit(1)

        print(f"\n[*] Executing task-finish workflow for task {args.task_id}...")
        cleanup_success_overall = True

        for h in manifest.get("hosts", {}).keys():
            try:
                # 1. Postflight inventory
                TaskManifestManager.record_postflight(args.task_id, h)

                # 2. Cleanup if requested
                if args.cleanup:
                    clean_res = execute_task_cleanup(h, args.task_id, apply=args.apply)
                    if not clean_res.get("cleanup_success", True):
                        cleanup_success_overall = False
                    manifest["hosts"][h]["cleanup_result"] = clean_res
                    # Fresh postflight verification after cleanup
                    try:
                        TaskManifestManager.record_postflight(args.task_id, h)
                    except HostCollectionError as exc:
                        print(f"[-] Post-cleanup verification failed on {h}: {exc}", file=sys.stderr)
                        manifest["hosts"][h]["cleanup_result"]["verification"] = "UNVERIFIED"
                        cleanup_success_overall = False
            except HostCollectionError as exc:
                print(f"[-] Host collection failed on {h}: {exc}", file=sys.stderr)
                cleanup_success_overall = False

        manifest["finish_time"] = datetime.now(timezone.utc).isoformat()
        TaskManifestManager.save_manifest(manifest)

        # 3. Final Task Gate
        try:
            report = audit_task_completion_gate(args.task_id)
        except HostCollectionError as exc:
            print(f"[-] Task completion gate failed: {exc}", file=sys.stderr)
            sys.exit(1)

        print("\n=== TASK COMPLETION HYGIENE GATE AUDIT ===")
        for h, data in report.get("hosts", {}).items():
            print(f"\n[{h.upper()}]")
            print(f"  temporary_containers:         {len(data.get('temporary_containers', []))} {data.get('temporary_containers', [])}")
            print(f"  superseded_candidate_images:  {len(data.get('superseded_candidate_images', []))} {data.get('superseded_candidate_images', [])}")
            print(f"  task_owned_dangling_images:   {len(data.get('task_owned_dangling_images', []))}")
            print(f"  disposable_builders:          {len(data.get('disposable_builders', []))}")
            print(f"  temporary_networks:           {len(data.get('temporary_networks', []))} {data.get('temporary_networks', [])}")
            print(f"  temporary_volumes:            {len(data.get('temporary_volumes', []))} {data.get('temporary_volumes', [])}")
            print(f"  approved_shared_cache_delta:  {data.get('approved_shared_cache_delta', '0.00GB')}")
            print(f"  unattributed_cache_delta:     {data.get('unattributed_cache_delta', '0.00GB')}")
            print(f"  host_build_cache_total:       {data.get('host_build_cache_total')}")
            if data.get("cache_warning"):
                print(f"  cache_warning:                {data.get('cache_warning')}")
            print(f"  Pre-existing unknown resources: {data.get('preexisting_unknown_resources')}")
            print(f"  New unknown resources:          {len(data.get('new_unknown_resources', []))} {data.get('new_unknown_resources', [])}")
            print(f"  Garbage Status:               {data.get('garbage_status')}")

        passed = report.get("gate_passed", False) and cleanup_success_overall
        print(f"\nOverall Task Gate Status: {'PASS' if passed else 'FAIL'}")
        if not passed:
            sys.exit(1)
        return

    if args.command == "audit-host":
        hosts = ["unraid", "gau-unraid"] if args.host == "all" else [args.host]
        all_complete = True
        for h in hosts:
            try:
                res = audit_host_posture(h)
                if args.json:
                    print(json.dumps(res, indent=2))
                else:
                    print(f"\n=== {h.upper()} HOST POSTURE AUDIT ===")
                    print(f"Observation status: {res['observation_status']}")
                    print(f"Total Containers:   {res['total_containers']}")
                    print(f"Total Images:       {res['total_images']}")
                    print(f"Total Volumes:      {res['total_volumes']}")
                    print(f"Total Networks:     {res['total_networks']}")
                    print(f"Production items:   {len(res['production_resources'])}")
                    print(f"Rollback items:     {len(res['rollback_resources'])}")
                    print(f"Active candidates:  {len(res['active_candidates'])}")
                    print(f"Other projects:     {res['other_projects_count']}")
                    print(f"Pre-existing unknown resources: {res['preexisting_unknown_count']}")
                    print(f"Host Build Cache:   {res['buildkit_cache']['total']} (reclaimable: {res['buildkit_cache']['reclaimable']})")
                    print("Status:             AUDIT COMPLETE")
            except HostCollectionError as exc:
                all_complete = False
                print(f"\n=== {h.upper()} HOST POSTURE AUDIT ===", file=sys.stderr)
                print("Observation status: INDETERMINATE", file=sys.stderr)
                print(f"Failure reason:     {exc}", file=sys.stderr)
        if not all_complete:
            sys.exit(1)
        return

    if args.command == "policy-check":
        is_synced = verify_policy_sync()
        if is_synced:
            print("[+] Workspace AGENTS.md matches canonical policy.")
            sys.exit(0)
        else:
            print("[-] Workspace AGENTS.md is out of sync with canonical policy. Run 'policy-install' to sync.", file=sys.stderr)
            sys.exit(1)

    if args.command == "policy-install":
        install_policy_to_workspace()
        return

    if args.command == "bootstrap-gau":
        try:
            res = bootstrap_gau_unraid()
            print(json.dumps(res, indent=2))
        except HostCollectionError as exc:
            print(f"[-] Bootstrap failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
