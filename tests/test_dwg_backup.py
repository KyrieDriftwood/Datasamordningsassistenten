"""Tester för dwg/backup.py: backup-kopior av DWG-filer.

Enhetstester mot syntetiska filer i tmp_path - kräver varken
accoreconsole eller riktiga DWG-fixturer, eftersom backup-logiken
bara handlar om filkopiering och sökvägsberäkning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dwg.backup import BACKUP_DIR_NAME, BackupError, backup_path_for, create_backup, has_backup, restore_backup


def test_backup_dir_is_hidden_dot_directory():
    """TB (CBE) kräver att backupkopian ligger i en dold punkt-mapp."""
    assert BACKUP_DIR_NAME.startswith(".")


def test_backup_path_for_mirrors_relative_path_under_project_root(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "Ritningar" / "X.dwg"
    expected = project_root / BACKUP_DIR_NAME / "Ritningar" / "X.dwg"
    assert backup_path_for(project_root, dwg_path) == expected


def test_backup_path_for_falls_back_to_filename_when_outside_project_root(tmp_path):
    project_root = tmp_path / "projekt"
    outside_path = tmp_path / "annan-mapp" / "X.dwg"
    expected = project_root / BACKUP_DIR_NAME / "X.dwg"
    assert backup_path_for(project_root, outside_path) == expected


def test_create_backup_copies_file_content(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "Ritningar" / "X.dwg"
    dwg_path.parent.mkdir(parents=True)
    dwg_path.write_bytes(b"ursprungligt DWG-innehall")

    backup = create_backup(project_root, dwg_path)

    assert backup == project_root / BACKUP_DIR_NAME / "Ritningar" / "X.dwg"
    assert backup.read_bytes() == b"ursprungligt DWG-innehall"
    assert has_backup(project_root, dwg_path)


def test_create_backup_overwrites_previous_backup(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "X.dwg"
    dwg_path.parent.mkdir(parents=True)

    dwg_path.write_bytes(b"version 1")
    create_backup(project_root, dwg_path)

    dwg_path.write_bytes(b"version 2")
    backup = create_backup(project_root, dwg_path)

    assert backup.read_bytes() == b"version 2"


def test_create_backup_raises_when_source_missing(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "saknas.dwg"
    with pytest.raises(BackupError):
        create_backup(project_root, dwg_path)


def test_restore_backup_copies_backup_back_over_original(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "X.dwg"
    dwg_path.parent.mkdir(parents=True)
    dwg_path.write_bytes(b"original")
    create_backup(project_root, dwg_path)

    dwg_path.write_bytes(b"korrupt efter misslyckad skrivning")
    restore_backup(project_root, dwg_path)

    assert dwg_path.read_bytes() == b"original"


def test_restore_backup_raises_when_no_backup_exists(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "X.dwg"
    dwg_path.parent.mkdir(parents=True)
    dwg_path.write_bytes(b"original")

    with pytest.raises(BackupError):
        restore_backup(project_root, dwg_path)


def test_restore_backup_retries_when_target_is_temporarily_locked(tmp_path, monkeypatch):
    import dwg.backup as backup_module

    project_root = tmp_path / "projekt"
    dwg_path = project_root / "X.dwg"
    dwg_path.parent.mkdir(parents=True)
    dwg_path.write_bytes(b"original")
    create_backup(project_root, dwg_path)

    real_copy2 = backup_module.shutil.copy2
    attempts = 0

    def copy_with_temporary_lock(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(13, "Filen används av en annan process")
        return real_copy2(source, target)

    monkeypatch.setattr(backup_module.shutil, "copy2", copy_with_temporary_lock)
    monkeypatch.setattr(backup_module.time, "sleep", lambda _seconds: None)

    dwg_path.write_bytes(b"korrupt efter misslyckad skrivning")
    restore_backup(project_root, dwg_path)

    assert attempts == 3
    assert dwg_path.read_bytes() == b"original"


def test_has_backup_false_when_no_backup_created(tmp_path):
    project_root = tmp_path / "projekt"
    dwg_path = project_root / "X.dwg"
    assert not has_backup(project_root, dwg_path)
