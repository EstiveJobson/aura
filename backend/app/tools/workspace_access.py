from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.core.config import WorkspaceWriteMode

_AURA_DOCKER_VOLUME_ROOT = re.compile(r"^/(?:[^/]+/)*docker/volumes/[^/]+/_data$")
_SUPPORTED_LOCAL_FILESYSTEMS = frozenset({"btrfs", "ext4", "xfs"})


class WorkspaceWriteUnsupported(RuntimeError):
    """WRITE was requested outside AURA's supported exclusive workspace boundary."""


class WorkspaceWriteAccess(Protocol):
    def assert_write_allowed(self) -> None: ...


@dataclass(frozen=True)
class ReadOnlyWorkspaceAccess:
    reason: str = "Workspace WRITE is disabled because this runtime is read-only."

    def assert_write_allowed(self) -> None:
        raise WorkspaceWriteUnsupported(self.reason)


@dataclass(frozen=True)
class DockerManagedWorkspaceAccess:
    workspace_root: Path
    mount_identity: tuple[str, str, str, str]

    @classmethod
    def establish(cls, workspace_root: Path) -> DockerManagedWorkspaceAccess:
        if not sys.platform.startswith("linux"):
            raise WorkspaceWriteUnsupported(
                "Docker-managed workspace WRITE is supported only by the Linux container runtime."
            )

        resolved_root = workspace_root.resolve(strict=True)
        if resolved_root != Path("/workspace"):
            raise WorkspaceWriteUnsupported(
                "Docker-managed workspace WRITE requires WORKSPACE_ROOT=/workspace."
            )
        app_root = Path("/app").resolve(strict=True)
        if (
            resolved_root == app_root
            or resolved_root in app_root.parents
            or app_root in resolved_root.parents
        ):
            raise WorkspaceWriteUnsupported("The workspace must be separate from /app.")

        mount_identity = _docker_volume_mount_identity(resolved_root)
        return cls(workspace_root=resolved_root, mount_identity=mount_identity)

    def assert_write_allowed(self) -> None:
        try:
            current = _docker_volume_mount_identity(self.workspace_root)
        except (OSError, WorkspaceWriteUnsupported) as exc:
            raise WorkspaceWriteUnsupported(
                "The Docker-managed workspace WRITE boundary is no longer valid."
            ) from exc
        if current != self.mount_identity:
            raise WorkspaceWriteUnsupported(
                "The Docker-managed workspace mount identity has changed."
            )


def build_workspace_write_access(
    workspace_root: Path,
    mode: WorkspaceWriteMode,
) -> WorkspaceWriteAccess:
    if mode is WorkspaceWriteMode.READ_ONLY:
        return ReadOnlyWorkspaceAccess()
    return DockerManagedWorkspaceAccess.establish(workspace_root)


def _docker_volume_mount_identity(workspace_root: Path) -> tuple[str, str, str, str]:
    """Return stable mount identity for a validated writable Docker volume."""

    mount_point = os.fsencode(workspace_root).decode(errors="strict")
    try:
        mount_lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkspaceWriteUnsupported("Linux mount information is unavailable.") from exc

    for line in mount_lines:
        before, separator, after = line.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        filesystem_fields = after.split()
        if len(fields) < 6 or len(filesystem_fields) < 1:
            continue
        root = _unescape_mount_field(fields[3])
        candidate_mount = _unescape_mount_field(fields[4])
        filesystem_type = filesystem_fields[0]
        if candidate_mount != mount_point:
            continue
        if not _AURA_DOCKER_VOLUME_ROOT.fullmatch(root):
            raise WorkspaceWriteUnsupported(
                "The workspace is not mounted from a Docker-managed volume."
            )
        if filesystem_type not in _SUPPORTED_LOCAL_FILESYSTEMS:
            raise WorkspaceWriteUnsupported(
                "The Docker workspace filesystem is not in AURA's local filesystem allowlist."
            )
        if "rw" not in fields[5].split(","):
            raise WorkspaceWriteUnsupported("The Docker workspace mount is not writable.")
        if not os.path.ismount(workspace_root):
            raise WorkspaceWriteUnsupported("The workspace is not a distinct mount.")
        return fields[0], fields[2], root, filesystem_type

    raise WorkspaceWriteUnsupported("The workspace is not a distinct Docker-managed mount.")


def _unescape_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )
