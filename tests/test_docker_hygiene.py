import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.docker_hygiene import (
    TaskManifestManager,
    classify_resource_ownership,
    compute_inventory_delta,
    plan_task_cleanup,
    execute_task_cleanup,
    audit_task_completion_gate,
    verify_policy_sync,
    PINNED_TOOLCHAIN,
)


def test_classify_universal_labels():
    # 1. Direct universal lifecycle labels
    res_prod = {"labels": {"io.agent.lifecycle": "production", "io.agent.task": "task-1"}}
    cat, reason = classify_resource_ownership("container", res_prod)
    assert cat == "A_production"

    res_rollback = {"labels": {"io.agent.lifecycle": "rollback"}}
    cat, reason = classify_resource_ownership("image", res_rollback)
    assert cat == "B_rollback"

    res_candidate = {"labels": {"io.agent.lifecycle": "candidate"}}
    cat, reason = classify_resource_ownership("image", res_candidate)
    assert cat == "C_candidate"

    res_temp = {"labels": {"io.agent.lifecycle": "temporary"}}
    cat, reason = classify_resource_ownership("container", res_temp)
    assert cat == "D_transient"


def test_classify_candidate_progression():
    # Candidate superseded by active candidate in manifest
    manifest = {
        "task_id": "task-rel-1",
        "hosts": {
            "gau-unraid": {
                "active_candidates": ["app:v1.0.0-rc2"],
            }
        }
    }
    cand1 = {"host": "gau-unraid", "repository": "app", "tag": "v1.0.0-rc1", "labels": {"io.agent.lifecycle": "candidate"}}
    cat1, _ = classify_resource_ownership("image", cand1, task_id="task-rel-1", manifest=manifest, is_delta=True)
    assert cat1 == "D_transient"  # Superseded!

    cand2 = {"host": "gau-unraid", "repository": "app", "tag": "v1.0.0-rc2", "labels": {"io.agent.lifecycle": "candidate"}}
    cat2, _ = classify_resource_ownership("image", cand2, task_id="task-rel-1", manifest=manifest, is_delta=True)
    assert cat2 == "C_candidate"  # Active!


def test_classify_unknown_preexisting_vs_delta():
    mystery_res = {"id": "res123", "name": "mystery-item", "labels": {}}
    # When present in preflight:
    cat_pre, _ = classify_resource_ownership("container", mystery_res, is_delta=False)
    assert cat_pre == "G_preexisting_unknown"

    # When newly created in delta:
    cat_delta, _ = classify_resource_ownership("container", mystery_res, is_delta=True)
    assert cat_delta == "G_new_unknown"


def test_cross_host_task_manifest_preservation(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-multi-host-task"

    mock_inv_gau = {"timestamp": "2026-09-14T00:00:00Z", "containers": [], "images": [], "volumes": [], "networks": [], "builders": [], "buildkit_cache": {"total": "0B"}}
    mock_inv_unraid = {"timestamp": "2026-09-14T00:01:00Z", "containers": [{"full_id": "c1", "name": "prod", "labels": {}}], "images": [], "volumes": [], "networks": [], "builders": [], "buildkit_cache": {"total": "10GB"}}

    with patch("scripts.docker_hygiene.collect_full_inventory") as mock_collect:
        mock_collect.side_effect = lambda host: mock_inv_gau if host == "gau-unraid" else mock_inv_unraid

        # 1. Start on gau-unraid
        TaskManifestManager.init_task_host(task_id, "test-proj", "gau-unraid")
        m1 = TaskManifestManager.load_manifest(task_id)
        assert "gau-unraid" in m1["hosts"]
        assert "unraid" not in m1["hosts"]

        # 2. Start on unraid - must NOT overwrite gau-unraid
        TaskManifestManager.init_task_host(task_id, "test-proj", "unraid")
        m2 = TaskManifestManager.load_manifest(task_id)
        assert "gau-unraid" in m2["hosts"]
        assert "unraid" in m2["hosts"]

        # Both preflight files exist independently
        pre_gau = TaskManifestManager.load_preflight(task_id, "gau-unraid")
        pre_unraid = TaskManifestManager.load_preflight(task_id, "unraid")
        assert pre_gau is not None
        assert pre_unraid is not None
        assert pre_unraid["buildkit_cache"]["total"] == "10GB"


def test_delta_temporary_network_and_volume(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-net-vol"

    preflight = {
        "containers": [],
        "images": [],
        "volumes": [],
        "networks": [{"id": "n_bridge", "name": "bridge"}],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }
    postflight = {
        "containers": [],
        "images": [],
        "volumes": [{"name": "task-temp-vol", "labels": {"io.agent.lifecycle": "temporary"}}],
        "networks": [
            {"id": "n_bridge", "name": "bridge"},
            {"id": "n_temp", "name": "task-temp-net", "labels": {"io.agent.lifecycle": "temporary"}},
        ],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }

    host_dir = TaskManifestManager.get_host_dir(task_id, "gau-unraid")
    with open(host_dir / "preflight.json", "w", encoding="utf-8") as f:
        json.dump(preflight, f)

    manifest = {"task_id": task_id, "hosts": {"gau-unraid": {}}}
    TaskManifestManager.save_manifest(manifest)

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight):
        plan = plan_task_cleanup("gau-unraid", task_id)
        assert len(plan["networks_to_delete"]) == 1
        assert plan["networks_to_delete"][0]["name"] == "task-temp-net"
        assert len(plan["volumes_to_delete"]) == 1
        assert plan["volumes_to_delete"][0]["name"] == "task-temp-vol"

        gate_rep = audit_task_completion_gate(task_id)
        assert gate_rep["gate_passed"] is False
        assert len(gate_rep["hosts"]["gau-unraid"]["temporary_networks"]) == 1
        assert len(gate_rep["hosts"]["gau-unraid"]["temporary_volumes"]) == 1


def test_delta_new_unknown_fails_gate_while_preexisting_passes(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-unknowns"

    preflight = {
        "containers": [{"full_id": "c_pre_unk", "id": "c_pre_unk", "name": "pre-existing-mystery", "labels": {}}],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }
    # 1. When postflight only has pre-existing unknown -> Gate PASS
    postflight_clean = dict(preflight)
    host_dir = TaskManifestManager.get_host_dir(task_id, "gau-unraid")
    with open(host_dir / "preflight.json", "w", encoding="utf-8") as f:
        json.dump(preflight, f)
    manifest = {"task_id": task_id, "hosts": {"gau-unraid": {}}}
    TaskManifestManager.save_manifest(manifest)

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight_clean):
        gate_rep = audit_task_completion_gate(task_id)
        assert gate_rep["gate_passed"] is True
        assert gate_rep["hosts"]["gau-unraid"]["preexisting_unknown_resources"] == 1
        assert len(gate_rep["hosts"]["gau-unraid"]["new_unknown_resources"]) == 0

    # 2. When postflight introduces a NEW unknown container -> Gate FAIL
    postflight_dirty = {
        "containers": [
            {"full_id": "c_pre_unk", "id": "c_pre_unk", "name": "pre-existing-mystery", "labels": {}},
            {"full_id": "c_new_unk", "id": "c_new_unk", "name": "new-mystery-container", "labels": {}},
        ],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }
    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight_dirty):
        gate_rep2 = audit_task_completion_gate(task_id)
        assert gate_rep2["gate_passed"] is False
        assert len(gate_rep2["hosts"]["gau-unraid"]["new_unknown_resources"]) == 1


def test_batch_image_inspection_handles_large_sets():
    from scripts.docker_hygiene import collect_images

    # Generate 80 fake image lines
    lines = []
    for i in range(80):
        img_id = f"sha256:img_{i:04d}"
        lines.append(json.dumps({"Repository": f"repo_{i}", "Tag": "latest", "ID": img_id, "Digest": "", "Size": "10MB", "CreatedAt": ""}))
    raw_images_output = "\n".join(lines)

    # 80th image has task label
    def mock_run_ssh(host, cmd, timeout=35):
        if "docker images" in cmd:
            return MagicMock(returncode=0, stdout=raw_images_output)
        if "docker image inspect" in cmd:
            # Parse inspect targets
            parts = cmd.replace("docker image inspect", "").split()
            chunk_res = []
            for p in parts:
                lbl = {"io.agent.lifecycle": "temporary"} if p == "sha256:img_0079" else {}
                chunk_res.append({"Id": p, "Config": {"Labels": lbl}})
            return MagicMock(returncode=0, stdout=json.dumps(chunk_res))
        return MagicMock(returncode=1, stdout="", stderr="")

    with patch("scripts.docker_hygiene.run_ssh", side_effect=mock_run_ssh):
        images = collect_images("gau-unraid")
        assert len(images) == 80
        assert images[79]["labels"].get("io.agent.lifecycle") == "temporary"
        assert images[0]["labels"] == {}


def test_cleanup_failure_propagates_to_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-fail-cleanup"

    preflight = {"containers": [], "images": [], "volumes": [], "networks": [], "builders": [], "buildkit_cache": {}}
    current = {
        "containers": [{"full_id": "c_fail", "id": "c_fail", "name": "failed-cont", "labels": {"io.agent.lifecycle": "temporary"}}],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {},
    }
    host_dir = TaskManifestManager.get_host_dir(task_id, "gau-unraid")
    with open(host_dir / "preflight.json", "w", encoding="utf-8") as f:
        json.dump(preflight, f)
    manifest = {"task_id": task_id, "hosts": {"gau-unraid": {}}}
    TaskManifestManager.save_manifest(manifest)

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=current):
        with patch("scripts.docker_hygiene.run_ssh", return_value=MagicMock(returncode=1, stderr="Permission denied")):
            clean_res = execute_task_cleanup("gau-unraid", task_id, apply=True)
            assert clean_res["cleanup_success"] is False
            assert clean_res["executed_actions"][0]["success"] is False


def test_policy_sync():
    assert verify_policy_sync() is True
