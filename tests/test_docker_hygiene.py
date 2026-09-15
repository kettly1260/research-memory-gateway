import json
import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.docker_hygiene import (
    HostCollectionError,
    TaskManifestManager,
    audit_host_posture,
    audit_task_completion_gate,
    classify_resource_ownership,
    collect_containers,
    collect_full_inventory,
    collect_images,
    collect_networks,
    collect_system_df,
    collect_volumes,
    compute_inventory_delta,
    execute_task_cleanup,
    plan_task_cleanup,
    probe_host_reachability,
    run_ssh,
    verify_policy_sync,
    WORKSPACE_AGENTS_FILE,
    PINNED_TOOLCHAIN,
    GAU_CACHE_LIMITS,
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


def test_policy_sync(tmp_path: Path):
    if WORKSPACE_AGENTS_FILE.exists():
        assert verify_policy_sync() is True
    fake_agents = tmp_path / "AGENTS.md"
    fake_canonical = tmp_path / "POLICY.md"
    fake_canonical.write_text("TEST POLICY", encoding="utf-8")
    fake_agents.write_text(
        "header\n<!-- BEGIN MANAGED DOCKER HYGIENE POLICY -->\nTEST POLICY\n<!-- END MANAGED DOCKER HYGIENE POLICY -->\nfooter",
        encoding="utf-8",
    )
    assert verify_policy_sync(agents_file=fake_agents, canonical_file=fake_canonical) is True


def test_fail_closed_ssh_failure():
    with patch("scripts.docker_hygiene.run_ssh") as mock_ssh:
        mock_ssh.return_value = MagicMock(
            returncode=255,
            stdout="",
            stderr="ssh: connect to host 192.168.22.202 port 22: Connection refused",
        )
        with pytest.raises(HostCollectionError) as exc_info:
            probe_host_reachability("gau-unraid")
        assert exc_info.value.returncode == 255
        assert "Connection refused" in exc_info.value.stderr

        with pytest.raises(HostCollectionError):
            collect_containers("gau-unraid")

        with pytest.raises(HostCollectionError):
            audit_host_posture("gau-unraid")


def test_fail_closed_docker_daemon_failure():
    with patch("scripts.docker_hygiene.run_ssh") as mock_ssh:
        mock_ssh.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///run/user/1000/docker.sock. Is the docker daemon running?",
        )
        with pytest.raises(HostCollectionError) as exc_info:
            probe_host_reachability("gau-unraid")
        assert "Cannot connect to the Docker daemon" in exc_info.value.stderr
        assert exc_info.value.reason == "Daemon/SSH Reachability Probe Failed"


def test_fail_closed_subprocess_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh gau-unraid", timeout=35)):
        with pytest.raises(HostCollectionError) as exc_info:
            run_ssh("gau-unraid", "docker ps")
        assert exc_info.value.returncode == -1
        assert "timed out" in exc_info.value.stderr
        assert exc_info.value.reason == "SSH Command Timeout"


def test_fail_closed_malformed_json():
    # 1. Reachability probe malformed JSON
    with patch("scripts.docker_hygiene.run_ssh") as mock_ssh:
        mock_ssh.return_value = MagicMock(returncode=0, stdout="<html>Error 502</html>", stderr="")
        with pytest.raises(HostCollectionError) as exc_info:
            probe_host_reachability("gau-unraid")
        assert "Malformed JSON" in exc_info.value.reason

    # 2. Containers inspect malformed JSON
    def mock_ssh_cont(host, cmd, timeout=35):
        if "docker ps -aq" in cmd:
            return MagicMock(returncode=0, stdout="c123456", stderr="")
        if "docker inspect" in cmd:
            return MagicMock(returncode=0, stdout="Not a JSON", stderr="")
        return MagicMock(returncode=0, stdout="{}", stderr="")

    with patch("scripts.docker_hygiene.run_ssh", side_effect=mock_ssh_cont):
        with pytest.raises(HostCollectionError) as exc_info:
            collect_containers("gau-unraid")
        assert "Malformed JSON" in exc_info.value.reason

    # 3. Volumes inspect malformed JSON
    def mock_ssh_vol(host, cmd, timeout=35):
        if "docker volume ls -q" in cmd:
            return MagicMock(returncode=0, stdout="vol1", stderr="")
        if "docker volume inspect" in cmd:
            return MagicMock(returncode=0, stdout="corrupted {json", stderr="")
        return MagicMock(returncode=0, stdout="[]", stderr="")

    with patch("scripts.docker_hygiene.run_ssh", side_effect=mock_ssh_vol):
        with pytest.raises(HostCollectionError) as exc_info:
            collect_volumes("gau-unraid")
        assert "Malformed JSON" in exc_info.value.reason

    # 4. Networks inspect malformed JSON
    def mock_ssh_net(host, cmd, timeout=35):
        if "docker network ls -q" in cmd:
            return MagicMock(returncode=0, stdout="net1", stderr="")
        if "docker network inspect" in cmd:
            return MagicMock(returncode=0, stdout="{bad json", stderr="")
        return MagicMock(returncode=0, stdout="[]", stderr="")

    with patch("scripts.docker_hygiene.run_ssh", side_effect=mock_ssh_net):
        with pytest.raises(HostCollectionError) as exc_info:
            collect_networks("gau-unraid")
        assert "Malformed JSON" in exc_info.value.reason

    # 5. System df malformed JSON
    with patch("scripts.docker_hygiene.run_ssh") as mock_ssh:
        mock_ssh.return_value = MagicMock(returncode=0, stdout="non-json df output\n", stderr="")
        with pytest.raises(HostCollectionError) as exc_info:
            collect_system_df("gau-unraid")
        assert "Malformed JSON" in exc_info.value.reason


def test_truly_empty_host_passes_audit():
    def mock_empty_ssh(host, cmd, timeout=35):
        if "docker version" in cmd:
            return MagicMock(returncode=0, stdout=json.dumps({"Server": {"Version": "27.0.0"}}), stderr="")
        if "docker ps -aq" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "docker images" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "docker volume ls -q" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "docker network ls -q" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "docker buildx ls" in cmd:
            return MagicMock(returncode=0, stdout="default * docker\n", stderr="")
        if "docker buildx du" in cmd:
            return MagicMock(returncode=0, stdout="Reclaimable: 0B\nTotal: 0B\n", stderr="")
        if "docker system df" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("scripts.docker_hygiene.run_ssh", side_effect=mock_empty_ssh):
        inv = collect_full_inventory("gau-unraid")
        assert inv["observation_complete"] is True
        assert len(inv["containers"]) == 0
        assert len(inv["images"]) == 0
        assert len(inv["volumes"]) == 0
        assert len(inv["networks"]) == 0

        posture = audit_host_posture("gau-unraid")
        assert posture["observation_status"] == "COMPLETE"
        assert posture["total_containers"] == 0
        assert posture["total_images"] == 0
        assert posture["total_volumes"] == 0
        assert posture["total_networks"] == 0
        assert posture["buildkit_cache"]["total"] == "0B"


def test_candidate_progression_full_inventory_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-candidate-lifecycle"

    preflight = {
        "timestamp": "2026-09-14T00:00:00Z",
        "containers": [],
        "images": [
            {
                "id": "sha256:base",
                "full_id": "sha256:base",
                "repository": "alpine",
                "tag": "latest",
                "size": "5MB",
                "labels": {},
            }
        ],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }

    # Postflight with rc1 (superseded) and rc2 (active)
    img_rc1 = {
        "id": "sha256:rc1",
        "full_id": "sha256:rc1",
        "repository": "research-memory-gateway",
        "tag": "v0.2.7-rc1",
        "size": "500MB",
        "host": "gau-unraid",
        "labels": {
            "io.agent.managed": "true",
            "io.agent.lifecycle": "candidate",
            "io.agent.task": task_id,
            "io.agent.project": "research-memory-gateway",
        },
    }
    img_rc2 = {
        "id": "sha256:rc2",
        "full_id": "sha256:rc2",
        "repository": "research-memory-gateway",
        "tag": "v0.2.7-rc2",
        "size": "500MB",
        "host": "gau-unraid",
        "labels": {
            "io.agent.managed": "true",
            "io.agent.lifecycle": "candidate",
            "io.agent.task": task_id,
            "io.agent.project": "research-memory-gateway",
        },
    }

    postflight = {
        "timestamp": "2026-09-14T01:00:00Z",
        "containers": [],
        "images": [preflight["images"][0], img_rc1, img_rc2],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }

    host_dir = TaskManifestManager.get_host_dir(task_id, "gau-unraid")
    with open(host_dir / "preflight.json", "w", encoding="utf-8") as f:
        json.dump(preflight, f)

    manifest = {
        "task_id": task_id,
        "project": "research-memory-gateway",
        "hosts": {
            "gau-unraid": {
                "active_candidates": ["research-memory-gateway:v0.2.7-rc2"],
                "cache_policy": "default",
            }
        },
    }
    TaskManifestManager.save_manifest(manifest)

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight):
        # 1. Cleanup plan should target rc1 (superseded) but protect rc2
        plan = plan_task_cleanup("gau-unraid", task_id)
        img_del_tags = [i["tag"] for i in plan["images_to_delete"]]
        assert "research-memory-gateway:v0.2.7-rc1" in img_del_tags
        assert "research-memory-gateway:v0.2.7-rc2" not in img_del_tags

        protected_tags = [p.get("tag") for p in plan["protected_skipped"]]
        assert "research-memory-gateway:v0.2.7-rc2" in protected_tags

        # 2. Before cleanup: gate fails because superseded candidate is still present
        gate_rep = audit_task_completion_gate(task_id)
        assert gate_rep["gate_passed"] is False
        assert "research-memory-gateway:v0.2.7-rc1" in gate_rep["hosts"]["gau-unraid"]["superseded_candidate_images"]

    # 3. After cleaning rc1: postflight has only base and rc2 -> gate PASSES!
    postflight_after_clean = {
        "timestamp": "2026-09-14T01:30:00Z",
        "containers": [],
        "images": [preflight["images"][0], img_rc2],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }
    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight_after_clean):
        gate_rep2 = audit_task_completion_gate(task_id)
        assert gate_rep2["gate_passed"] is True
        assert len(gate_rep2["hosts"]["gau-unraid"]["superseded_candidate_images"]) == 0


def test_volume_and_network_reference_protection(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-ref-protection"

    preflight = {
        "containers": [],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }

    # Container mounting the temporary volume and attached to the network
    running_cont = {
        "id": "c_running",
        "full_id": "c_running_full",
        "name": "active_worker",
        "running": True,
        "labels": {"io.agent.lifecycle": "production"},
        "mounts": [
            {"type": "volume", "name": "shared_data_vol", "source": "/var/lib/docker/volumes/shared_data_vol/_data"}
        ],
        "networks": ["shared_app_net"],
    }
    vol = {
        "name": "shared_data_vol",
        "labels": {"io.agent.lifecycle": "temporary", "io.agent.task": task_id},
    }
    net = {
        "id": "n_app",
        "full_id": "n_app_full",
        "name": "shared_app_net",
        "labels": {"io.agent.lifecycle": "temporary", "io.agent.task": task_id},
    }

    postflight = {
        "containers": [running_cont],
        "images": [],
        "volumes": [vol],
        "networks": [net],
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
        # Volume and network must NOT be in delete lists
        assert len(plan["volumes_to_delete"]) == 0
        assert len(plan["networks_to_delete"]) == 0

        # They must be recorded in protected_skipped
        skipped_names = [p.get("name") for p in plan["protected_skipped"]]
        assert "shared_data_vol" in skipped_names
        assert "shared_app_net" in skipped_names

        # Reason must mention reference
        vol_skip = [p for p in plan["protected_skipped"] if p.get("name") == "shared_data_vol"][0]
        assert "Referenced by running/protected container" in vol_skip["reason"]


def test_unattributed_vs_approved_cache_delta(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    task_id = "test-cache-gate"

    preflight = {
        "containers": [],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "0B"},
    }
    postflight_5gb = {
        "containers": [],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "5.00GB", "reclaimable": "5.00GB"},
    }

    host_dir = TaskManifestManager.get_host_dir(task_id, "gau-unraid")
    with open(host_dir / "preflight.json", "w", encoding="utf-8") as f:
        json.dump(preflight, f)

    # 1. Policy = default -> 5GB delta is UNATTRIBUTED -> FAIL
    manifest_default = {
        "task_id": task_id,
        "hosts": {
            "gau-unraid": {
                "cache_policy": "default",
            }
        },
    }
    TaskManifestManager.save_manifest(manifest_default)
    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight_5gb):
        gate_default = audit_task_completion_gate(task_id)
        assert gate_default["gate_passed"] is False
        gau_rep = gate_default["hosts"]["gau-unraid"]
        assert gau_rep["unattributed_cache_delta"] == "5.00GB"
        assert gau_rep["approved_shared_cache_delta"] == "0.00GB"

    # 2. Policy = approved_shared -> 5GB delta is APPROVED -> PASS
    manifest_approved = {
        "task_id": task_id,
        "hosts": {
            "gau-unraid": {
                "cache_policy": "approved_shared",
            }
        },
    }
    TaskManifestManager.save_manifest(manifest_approved)
    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight_5gb):
        gate_approved = audit_task_completion_gate(task_id)
        assert gate_approved["gate_passed"] is True
        gau_rep = gate_approved["hosts"]["gau-unraid"]
        assert gau_rep["unattributed_cache_delta"] == "0.00GB"
        assert gau_rep["approved_shared_cache_delta"] == "5.00GB"

    # 3. Policy = approved_shared but exceeds hard limit (26GB > 25GB) -> FAIL
    postflight_26gb = {
        "containers": [],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
        "buildkit_cache": {"total": "26.00GB", "reclaimable": "26.00GB"},
    }
    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=postflight_26gb):
        gate_hard_limit = audit_task_completion_gate(task_id)
        assert gate_hard_limit["gate_passed"] is False
        gau_rep = gate_hard_limit["hosts"]["gau-unraid"]
        assert "EXCEEDED HARD LIMIT" in gau_rep["cache_warning"]
