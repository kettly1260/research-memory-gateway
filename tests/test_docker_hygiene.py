import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.docker_hygiene import (
    TaskManifestManager,
    classify_resource_ownership,
    plan_cleanup,
    execute_cleanup,
    audit_release_gate,
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


def test_classify_with_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.docker_hygiene.HYGIENE_BASE_DIR", tmp_path)
    manifest = {
        "task_id": "test-task-123",
        "created_containers": ["c-test-1"],
        "created_images": ["img-test-1"],
    }
    # Container matches created in manifest
    res = {"id": "c-test-1", "name": "canary-worker", "labels": {}}
    cat, reason = classify_resource_ownership("container", res, manifest=manifest)
    assert cat == "D_transient"
    assert "test-task-123" in reason

    # Resource with task label matching manifest
    res2 = {"id": "c-other", "labels": {"io.agent.task": "test-task-123"}}
    cat, reason = classify_resource_ownership("container", res2, manifest=manifest)
    assert cat == "D_transient"


def test_classify_foreign_projects():
    res_immich = {"name": "immich-server", "image": "ghcr.io/imagegenius/immich:noml", "labels": {}}
    cat, reason = classify_resource_ownership("container", res_immich)
    assert cat == "F_other_projects"

    res_wechat = {"name": "wechat-hub-core", "repository": "ghcr.io/onestao/wechat-hub-core", "labels": {}}
    cat, reason = classify_resource_ownership("image", res_wechat)
    assert cat == "F_other_projects"


def test_classify_unknown_resources():
    res_unknown = {"id": "random123", "name": "mystery-container", "labels": {}}
    cat, reason = classify_resource_ownership("container", res_unknown)
    assert cat == "G_ownership_unknown"


def test_safe_cleanup_dry_run_default():
    mock_inv = {
        "containers": [
            {"id": "c1", "name": "transient-worker", "labels": {"io.agent.lifecycle": "temporary"}},
            {"id": "c2", "name": "prod-app", "labels": {"io.agent.lifecycle": "production"}},
            {"id": "c3", "name": "unknown-app", "labels": {}},
        ],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
    }

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=mock_inv):
        plan = plan_cleanup("unraid")
        assert plan["dry_run"] is True
        assert len(plan["containers_to_delete"]) == 1
        assert plan["containers_to_delete"][0]["id"] == "c1"
        assert len(plan["protected_skipped"]) == 1
        assert plan["protected_skipped"][0]["id"] == "c2"
        assert len(plan["unknown_skipped"]) == 1
        assert plan["unknown_skipped"][0]["id"] == "c3"


def test_execute_cleanup_requires_apply():
    mock_inv = {
        "containers": [
            {"id": "c1", "name": "transient-worker", "labels": {"io.agent.lifecycle": "temporary"}},
        ],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [],
    }

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=mock_inv):
        # Default without apply must be dry-run, run_ssh should not be called
        with patch("scripts.docker_hygiene.run_ssh") as mock_ssh:
            res = execute_cleanup("unraid", apply=False)
            assert res["dry_run"] is True
            mock_ssh.assert_not_called()

        # With apply=True, run_ssh must be called to delete
        with patch("scripts.docker_hygiene.run_ssh", return_value=MagicMock(returncode=0, stdout="c1\n")) as mock_ssh:
            res = execute_cleanup("unraid", apply=True)
            assert res["dry_run"] is False
            assert "executed_actions" in res
            assert len(res["executed_actions"]) == 1
            mock_ssh.assert_called_once_with("unraid", "docker rm c1")


def test_audit_release_gate_evaluates_all_fields():
    mock_clean_inv = {
        "containers": [{"id": "c_prod", "name": "prod", "labels": {"io.agent.lifecycle": "production"}}],
        "images": [{"id": "img_prod", "repository": "app", "tag": "1.0", "labels": {"io.agent.lifecycle": "production"}}],
        "volumes": [],
        "networks": [],
        "builders": [{"name": "default"}],
        "buildkit_cache": {"total": "5GB", "reclaimable": "5GB"},
    }

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=mock_clean_inv):
        report = audit_release_gate(host_filter="unraid")
        assert report["gate_passed"] is True
        unraid_host = report["hosts"]["unraid"]
        assert unraid_host["garbage_status"] == "NONE"
        assert len(unraid_host["temporary_containers"]) == 0
        assert len(unraid_host["superseded_candidate_images"]) == 0
        assert len(unraid_host["unknown_resources"]) == 0


def test_audit_release_gate_fails_on_transient_or_unknown():
    mock_dirty_inv = {
        "containers": [
            {"id": "c_temp", "name": "temp-box", "labels": {"io.agent.lifecycle": "temporary"}},
        ],
        "images": [],
        "volumes": [],
        "networks": [],
        "builders": [{"name": "rmg-build-disposable-1"}],
        "buildkit_cache": {"total": "0B"},
    }

    with patch("scripts.docker_hygiene.collect_full_inventory", return_value=mock_dirty_inv):
        report = audit_release_gate(host_filter="unraid")
        assert report["gate_passed"] is False
        unraid_host = report["hosts"]["unraid"]
        assert unraid_host["garbage_status"] == "FAIL"
        assert len(unraid_host["temporary_containers"]) == 1
        assert len(unraid_host["disposable_builders"]) == 1


def test_pinned_toolchain_checksums_defined():
    assert "buildx" in PINNED_TOOLCHAIN
    assert PINNED_TOOLCHAIN["buildx"]["version"] == "v0.37.1"
    assert len(PINNED_TOOLCHAIN["buildx"]["sha256"]) == 64

    assert "compose" in PINNED_TOOLCHAIN
    assert PINNED_TOOLCHAIN["compose"]["version"] == "v5.5.1"
    assert len(PINNED_TOOLCHAIN["compose"]["sha256"]) == 64
