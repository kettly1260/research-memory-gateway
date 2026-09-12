from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from research_memory_gateway.config import (
    AppConfig,
    ConversationArchiveConfig,
    load_config,
    validate_safe_path,
)


def test_config_backward_compatibility(tmp_path: Path) -> None:
    config_data = {
        "server": {"port": 9000},
        "backend": {"type": "sqlite"},
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.safe_dump(config_data), encoding="utf-8")

    loaded = load_config(config_file)
    assert loaded.conversation_archive.enabled is False
    assert loaded.conversation_archive.staging_dir == "./exports/conversation-staging"
    assert loaded.conversation_archive.vault_root is None
    assert loaded.conversation_archive.require_explicit_vault_confirmation is True


def test_staging_dir_resolution(tmp_path: Path) -> None:
    cfg = ConversationArchiveConfig(staging_dir="./my_staging")
    resolved = cfg.resolve_staging_dir(base_dir=tmp_path)
    assert resolved == (tmp_path / "my_staging").resolve()

    abs_staging = (tmp_path / "abs_staging").resolve()
    cfg_abs = ConversationArchiveConfig(staging_dir=str(abs_staging))
    assert cfg_abs.resolve_staging_dir(base_dir=tmp_path) == abs_staging


def test_vault_root_guardrails(tmp_path: Path) -> None:
    cfg_no_vault = ConversationArchiveConfig(vault_root=None)
    with pytest.raises(ValueError, match="vault_root is not configured"):
        cfg_no_vault.resolve_vault_root()

    non_existent = tmp_path / "does_not_exist"
    cfg_with_vault = ConversationArchiveConfig(
        vault_root=str(non_existent),
        require_explicit_vault_confirmation=True,
    )
    with pytest.raises(PermissionError, match="Explicit confirmation is required"):
        cfg_with_vault.resolve_vault_root(confirmed=False)

    with pytest.raises(FileNotFoundError, match="does not exist.*Automatic creation of empty vault is forbidden"):
        cfg_with_vault.resolve_vault_root(confirmed=True)

    existing_vault = tmp_path / "real_vault"
    existing_vault.mkdir()
    cfg_real = ConversationArchiveConfig(
        vault_root=str(existing_vault),
        require_explicit_vault_confirmation=True,
    )
    assert cfg_real.resolve_vault_root(confirmed=True) == existing_vault.resolve()


def test_validate_safe_path(tmp_path: Path) -> None:
    root1 = tmp_path / "allowed1"
    root2 = tmp_path / "allowed2"
    root1.mkdir()
    root2.mkdir()

    safe_file = root1 / "notes" / "session.md"
    safe_file.parent.mkdir()
    safe_file.write_text("ok", encoding="utf-8")

    assert validate_safe_path(safe_file, [root1, root2]) == safe_file.resolve()

    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside allowed roots"):
        validate_safe_path(outside_file, [root1, root2])

    with pytest.raises(PermissionError, match="outside allowed roots"):
        validate_safe_path(root1 / ".." / "secret.txt", [root1, root2])


def test_canonical_root_resolution_and_traversal(tmp_path: Path) -> None:
    vault = tmp_path / "real_vault"
    vault.mkdir()

    cfg = ConversationArchiveConfig(
        vault_root=str(vault),
        canonical_subdir="90_System/AI-Memory",
        require_explicit_vault_confirmation=True,
    )

    with pytest.raises(PermissionError, match="Explicit confirmation is required"):
        cfg.resolve_canonical_root(confirmed=False)

    canonical_root = cfg.resolve_canonical_root(confirmed=True)
    assert canonical_root == (vault / "90_System" / "AI-Memory").resolve()

    cfg_escape = ConversationArchiveConfig(
        vault_root=str(vault),
        canonical_subdir="../escaped",
        require_explicit_vault_confirmation=True,
    )
    with pytest.raises(PermissionError, match="canonical_subdir escapes vault root"):
        cfg_escape.resolve_canonical_root(confirmed=True)


def test_manifest_and_index_path_resolution(tmp_path: Path) -> None:
    cfg = ConversationArchiveConfig(
        staging_dir="./exports/staging",
        manifest_path="./data/manifest.sqlite",
        index_path="./data/index.sqlite",
    )

    assert cfg.resolve_manifest_path(base_dir=tmp_path) == (tmp_path / "data" / "manifest.sqlite").resolve()
    archive_root = tmp_path / "archive"
    assert cfg.resolve_archive_manifest_path(archive_root) == (
        archive_root / ".ai-memory" / "manifest.sqlite"
    ).resolve()
    assert cfg.resolve_index_path(base_dir=tmp_path) == (tmp_path / "data" / "index.sqlite").resolve()

    cfg_staging_escape = ConversationArchiveConfig(staging_dir="../../escape")
    with pytest.raises(PermissionError, match="staging_dir escapes base directory"):
        cfg_staging_escape.resolve_staging_dir(base_dir=tmp_path)

    cfg_manifest_escape = ConversationArchiveConfig(manifest_path="../../escape.sqlite")
    with pytest.raises(PermissionError, match="manifest_path escapes base directory"):
        cfg_manifest_escape.resolve_manifest_path(base_dir=tmp_path)

    cfg_index_escape = ConversationArchiveConfig(index_path="../../escape.sqlite")
    with pytest.raises(PermissionError, match="index_path escapes base directory"):
        cfg_index_escape.resolve_index_path(base_dir=tmp_path)


def test_config_example_uses_archive_local_manifest_contract() -> None:
    example = Path("config.example.yaml").read_text(encoding="utf-8")
    assert "manifest_path:" not in example
    assert "<conversation_root>/.ai-memory/manifest.sqlite" in example
