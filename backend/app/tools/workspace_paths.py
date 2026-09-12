from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_RELATIVE_PATH_LENGTH = 500


class WorkspacePathError(ValueError):
    """A sanitized workspace-boundary validation failure."""


def validate_relative_path(value: str, *, allow_root: bool) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_RELATIVE_PATH_LENGTH or "\x00" in candidate:
        raise WorkspacePathError("The workspace-relative path is invalid.")

    portable = candidate.replace("\\", "/")
    posix_path = PurePosixPath(portable)
    windows_path = PureWindowsPath(candidate)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise WorkspacePathError("Absolute paths are not allowed.")
    if any(part == ".." for part in posix_path.parts):
        raise WorkspacePathError("Parent path traversal is not allowed.")

    normalized = posix_path.as_posix()
    if normalized in {"", "."}:
        if allow_root:
            return "."
        raise WorkspacePathError("The workspace root is not a valid file path.")
    return normalized


class WorkspaceBoundary:
    """Resolve only non-symlink paths contained by one configured root."""

    def __init__(self, workspace_root: Path) -> None:
        try:
            root = workspace_root.resolve(strict=True)
        except OSError as exc:
            raise WorkspacePathError("The configured workspace is unavailable.") from exc
        if not root.is_dir():
            raise WorkspacePathError("The configured workspace is unavailable.")
        self.root = root

    def resolve_existing(self, relative_path: str, *, allow_root: bool = False) -> Path:
        normalized = validate_relative_path(relative_path, allow_root=allow_root)
        raw_path = self._raw_path(normalized)
        self._reject_symlink_components(raw_path)
        try:
            resolved = raw_path.resolve(strict=True)
        except OSError as exc:
            raise WorkspacePathError("The requested workspace path is unavailable.") from exc
        self._require_contained(resolved)
        return resolved

    def resolve_destination(self, relative_path: str) -> Path:
        normalized = validate_relative_path(relative_path, allow_root=False)
        raw_path = self._raw_path(normalized)
        self._reject_symlink_components(raw_path.parent)
        try:
            resolved_parent = raw_path.parent.resolve(strict=True)
        except OSError as exc:
            raise WorkspacePathError("The destination directory is unavailable.") from exc
        self._require_contained(resolved_parent)
        if not resolved_parent.is_dir():
            raise WorkspacePathError("The destination directory is unavailable.")
        return resolved_parent / raw_path.name

    def is_safe_existing(self, path: Path) -> bool:
        try:
            self._reject_symlink_components(path)
            resolved = path.resolve(strict=True)
            self._require_contained(resolved)
        except (OSError, WorkspacePathError):
            return False
        return True

    def _raw_path(self, normalized: str) -> Path:
        if normalized == ".":
            return self.root
        return self.root.joinpath(*PurePosixPath(normalized).parts)

    def _reject_symlink_components(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError("The requested path is outside the workspace.") from exc
        current = self.root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise WorkspacePathError("Symbolic links are not allowed for workspace access.")

    def _require_contained(self, path: Path) -> None:
        if path != self.root and not path.is_relative_to(self.root):
            raise WorkspacePathError("The requested path is outside the workspace.")
