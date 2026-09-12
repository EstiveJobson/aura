from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

MAX_RELATIVE_PATH_LENGTH = 500

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    *(f"LPT{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
}


class WorkspacePathError(ValueError):
    """A sanitized workspace-boundary or platform-guarantee failure."""


class WorkspaceFileTooLarge(WorkspacePathError):
    """The acquired regular file exceeded its bounded read allowance."""


class FilesystemObjectIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    platform: Literal["linux", "windows"]
    volume_id: str = Field(min_length=1, max_length=64)
    object_id: str = Field(min_length=1, max_length=64)


class SourceFilePrecondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    identity: FilesystemObjectIdentity
    size_bytes: int = Field(ge=0)
    modified_ns: int = Field(ge=0)
    changed_ns: int = Field(ge=0)


class MoveApprovalContext(BaseModel):
    """Application-generated filesystem state bound to one pending move."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    workspace: FilesystemObjectIdentity
    source: SourceFilePrecondition


@dataclass(frozen=True)
class DirectoryBatch:
    names: tuple[str, ...]
    visited_entries: int
    truncated: bool


class _WorkspaceBackend(Protocol):
    root: Path
    workspace_name: str

    def approval_context(self, relative_source: str) -> MoveApprovalContext: ...

    def assert_workspace_identity(
        self, expected: FilesystemObjectIdentity | None = None
    ) -> None: ...

    def directory_batch(self, relative_path: str, visit_budget: int) -> DirectoryBatch: ...

    def entry_kind(self, relative_path: str) -> Literal["directory", "file"] | None: ...

    def read_regular_file(self, relative_path: str, max_bytes: int) -> bytes: ...

    def move_no_replace(
        self,
        source: str,
        destination: str,
        context: MoveApprovalContext,
    ) -> None: ...


def validate_relative_path(value: str, *, allow_root: bool) -> str:
    if os.name == "nt":
        raw_windows_path = PureWindowsPath(value)
        raw_posix_parts = PurePosixPath(value.replace("\\", "/")).parts
        if raw_windows_path.is_absolute() or raw_windows_path.drive:
            raise WorkspacePathError("Absolute paths are not allowed.")
        _validate_windows_components(raw_posix_parts)

    candidate = value.strip()
    if not candidate or len(candidate) > MAX_RELATIVE_PATH_LENGTH or "\x00" in candidate:
        raise WorkspacePathError("The workspace-relative path is invalid.")

    portable = candidate.replace("\\", "/")
    posix_path = PurePosixPath(portable)
    if posix_path.is_absolute():
        raise WorkspacePathError("Absolute paths are not allowed.")
    if any(part == ".." for part in posix_path.parts):
        raise WorkspacePathError("Parent path traversal is not allowed.")

    normalized = posix_path.as_posix()
    if normalized in {"", "."}:
        if allow_root:
            return "."
        raise WorkspacePathError("The workspace root is not a valid file path.")
    return normalized


def _validate_windows_components(parts: tuple[str, ...]) -> None:
    for component in parts:
        if component in {"", "."}:
            continue
        if ":" in component:
            raise WorkspacePathError("Windows alternate data streams are not allowed.")
        if component.endswith((".", " ")):
            raise WorkspacePathError("Ambiguous Windows path components are not allowed.")
        device_stem = component.split(".", maxsplit=1)[0].upper()
        if device_stem in _WINDOWS_RESERVED_NAMES:
            raise WorkspacePathError("Windows device names are not allowed.")


class WorkspaceBoundary:
    """Acquire workspace objects through a platform-specific, fail-closed boundary."""

    def __init__(self, workspace_root: Path) -> None:
        if sys.platform.startswith("linux"):
            self._backend: _WorkspaceBackend = _LinuxWorkspaceBackend(workspace_root)
        elif os.name == "nt":
            self._backend = _WindowsWorkspaceBackend(workspace_root)
        else:
            raise WorkspacePathError("Secure workspace access is unsupported on this platform.")

    @property
    def root(self) -> Path:
        return self._backend.root

    @property
    def workspace_name(self) -> str:
        return self._backend.workspace_name

    def approval_context(self, relative_source: str) -> dict[str, Any]:
        normalized = validate_relative_path(relative_source, allow_root=False)
        return self._backend.approval_context(normalized).model_dump(mode="json")

    def assert_workspace_identity(self, expected: FilesystemObjectIdentity | None = None) -> None:
        self._backend.assert_workspace_identity(expected)

    def directory_batch(self, relative_path: str, visit_budget: int) -> DirectoryBatch:
        normalized = validate_relative_path(relative_path, allow_root=True)
        if visit_budget < 1:
            raise WorkspacePathError("The directory traversal budget is exhausted.")
        return self._backend.directory_batch(normalized, visit_budget)

    def entry_kind(self, relative_path: str) -> Literal["directory", "file"] | None:
        normalized = validate_relative_path(relative_path, allow_root=True)
        return self._backend.entry_kind(normalized)

    def read_regular_file(self, relative_path: str, max_bytes: int) -> bytes:
        normalized = validate_relative_path(relative_path, allow_root=False)
        if max_bytes < 0:
            raise WorkspacePathError("The file read budget is invalid.")
        return self._backend.read_regular_file(normalized, max_bytes)

    def move_no_replace(
        self,
        source: str,
        destination: str,
        approval_context: dict[str, Any],
    ) -> None:
        normalized_source = validate_relative_path(source, allow_root=False)
        normalized_destination = validate_relative_path(destination, allow_root=False)
        try:
            context = MoveApprovalContext.model_validate(approval_context)
        except ValueError as exc:
            raise WorkspacePathError("The persisted filesystem approval is invalid.") from exc
        self._backend.move_no_replace(normalized_source, normalized_destination, context)


class _LinuxOpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    ]


class _LinuxWorkspaceBackend:
    _SYS_OPENAT2 = 437
    _O_DIRECTORY = 0o200000
    _O_CLOEXEC = 0o2000000
    _O_NOFOLLOW = 0o400000
    _O_PATH = 0o10000000
    _O_NONBLOCK = 0o4000
    _RESOLVE_NO_MAGICLINKS = 0x02
    _RESOLVE_NO_SYMLINKS = 0x04
    _RESOLVE_BENEATH = 0x08
    _RENAME_NOREPLACE = 1

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.absolute()
        self.workspace_name = self.root.name
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.syscall.restype = ctypes.c_long
        try:
            renameat2 = self._libc.renameat2
        except AttributeError as exc:
            raise WorkspacePathError(
                "Atomic no-overwrite moves are unsupported on this platform."
            ) from exc
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        self._renameat2 = renameat2
        self._root_fd = self._open_configured_root()
        self._selected_identity = self._identity_from_stat(os.fstat(self._root_fd))
        try:
            probe_fd = self._open_beneath(".", self._directory_flags())
        except Exception:
            os.close(self._root_fd)
            raise
        os.close(probe_fd)

    def __del__(self) -> None:
        root_fd = getattr(self, "_root_fd", -1)
        if isinstance(root_fd, int) and root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                pass
            self._root_fd = -1

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | _LinuxWorkspaceBackend._O_DIRECTORY
            | _LinuxWorkspaceBackend._O_CLOEXEC
            | _LinuxWorkspaceBackend._O_NOFOLLOW
        )

    @staticmethod
    def _metadata_flags() -> int:
        return (
            _LinuxWorkspaceBackend._O_PATH
            | _LinuxWorkspaceBackend._O_CLOEXEC
            | _LinuxWorkspaceBackend._O_NOFOLLOW
        )

    @staticmethod
    def _read_flags() -> int:
        return (
            os.O_RDONLY
            | _LinuxWorkspaceBackend._O_CLOEXEC
            | _LinuxWorkspaceBackend._O_NOFOLLOW
            | _LinuxWorkspaceBackend._O_NONBLOCK
        )

    def _open_configured_root(self) -> int:
        anchor_fd = -1
        try:
            anchor_fd = os.open("/", self._directory_flags())
            relative_root = self.root.as_posix().lstrip("/") or "."
            descriptor = self._open_at(anchor_fd, relative_root, self._directory_flags())
            acquired = os.fstat(descriptor)
            if not stat.S_ISDIR(acquired.st_mode):
                raise OSError(errno.ENOTDIR, "workspace root is not a directory")
            return descriptor
        except (OSError, WorkspacePathError) as exc:
            raise WorkspacePathError("The configured workspace is unavailable.") from exc
        finally:
            if anchor_fd >= 0:
                os.close(anchor_fd)

    def _open_beneath(self, relative_path: str, flags: int) -> int:
        return self._open_at(self._root_fd, relative_path, flags)

    def _open_at(self, directory_fd: int, relative_path: str, flags: int) -> int:
        how = _LinuxOpenHow(
            flags=flags,
            mode=0,
            resolve=(
                self._RESOLVE_BENEATH | self._RESOLVE_NO_MAGICLINKS | self._RESOLVE_NO_SYMLINKS
            ),
        )
        encoded_path = os.fsencode(relative_path)
        result = self._libc.syscall(
            self._SYS_OPENAT2,
            directory_fd,
            ctypes.c_char_p(encoded_path),
            ctypes.byref(how),
            ctypes.sizeof(how),
        )
        if result < 0:
            error_number = ctypes.get_errno()
            if error_number in {errno.ENOSYS, errno.E2BIG, errno.EINVAL}:
                raise WorkspacePathError(
                    "Secure workspace acquisition is unsupported on this platform."
                )
            raise WorkspacePathError("The requested workspace path is unavailable.")
        return int(result)

    @staticmethod
    def _identity_from_stat(result: os.stat_result) -> FilesystemObjectIdentity:
        return FilesystemObjectIdentity(
            platform="linux",
            volume_id=str(result.st_dev),
            object_id=str(result.st_ino),
        )

    @classmethod
    def _source_from_stat(cls, result: os.stat_result) -> SourceFilePrecondition:
        return SourceFilePrecondition(
            identity=cls._identity_from_stat(result),
            size_bytes=result.st_size,
            modified_ns=result.st_mtime_ns,
            changed_ns=result.st_ctime_ns,
        )

    def assert_workspace_identity(self, expected: FilesystemObjectIdentity | None = None) -> None:
        current_fd = self._open_configured_root()
        try:
            current = self._identity_from_stat(os.fstat(current_fd))
        finally:
            os.close(current_fd)
        if current != self._selected_identity or (expected is not None and current != expected):
            raise WorkspacePathError("The configured workspace identity has changed.")

    def approval_context(self, relative_source: str) -> MoveApprovalContext:
        self.assert_workspace_identity()
        source_fd = self._open_beneath(relative_source, self._metadata_flags())
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise WorkspacePathError("Only regular files can be moved.")
            source = self._source_from_stat(source_stat)
        finally:
            os.close(source_fd)
        return MoveApprovalContext(workspace=self._selected_identity, source=source)

    def directory_batch(self, relative_path: str, visit_budget: int) -> DirectoryBatch:
        self.assert_workspace_identity()
        directory_fd = self._open_beneath(relative_path, self._directory_flags())
        names: list[str] = []
        visited = 0
        truncated = False
        try:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    visited += 1
                    if visited == visit_budget:
                        truncated = True
                        break
                    names.append(entry.name)
        except OSError as exc:
            raise WorkspacePathError("The requested directory is unavailable.") from exc
        finally:
            os.close(directory_fd)
        names.sort(key=str.casefold)
        return DirectoryBatch(tuple(names), visited, truncated)

    def entry_kind(self, relative_path: str) -> Literal["directory", "file"] | None:
        try:
            descriptor = self._open_beneath(relative_path, self._metadata_flags())
        except WorkspacePathError:
            return None
        try:
            mode = os.fstat(descriptor).st_mode
        finally:
            os.close(descriptor)
        if stat.S_ISDIR(mode):
            return "directory"
        if stat.S_ISREG(mode):
            return "file"
        return None

    def read_regular_file(self, relative_path: str, max_bytes: int) -> bytes:
        self.assert_workspace_identity()
        descriptor = self._open_beneath(relative_path, self._read_flags())
        try:
            acquired = os.fstat(descriptor)
            if not stat.S_ISREG(acquired.st_mode):
                raise WorkspacePathError("The requested path is not a regular file.")
            payload = os.read(descriptor, max_bytes + 1)
        except OSError as exc:
            raise WorkspacePathError("The requested file is unavailable or unreadable.") from exc
        finally:
            os.close(descriptor)
        if len(payload) > max_bytes:
            raise WorkspaceFileTooLarge("The requested file exceeds the read limit.")
        return payload

    @contextmanager
    def _open_parent(self, relative_path: str) -> Iterator[tuple[int, str]]:
        path = PurePosixPath(relative_path)
        parent = path.parent.as_posix()
        parent_fd = self._open_beneath(parent, self._directory_flags())
        try:
            yield parent_fd, path.name
        finally:
            os.close(parent_fd)

    def move_no_replace(
        self,
        source: str,
        destination: str,
        context: MoveApprovalContext,
    ) -> None:
        self.assert_workspace_identity(context.workspace)
        source_fd = self._open_beneath(source, self._metadata_flags())
        try:
            acquired_source = os.fstat(source_fd)
            if not stat.S_ISREG(acquired_source.st_mode):
                raise WorkspacePathError("Only regular files can be moved.")
            if self._source_from_stat(acquired_source) != context.source:
                raise WorkspacePathError("The approved source file identity has changed.")

            with self._open_parent(source) as (source_parent_fd, source_name):
                with self._open_parent(destination) as (
                    destination_parent_fd,
                    destination_name,
                ):
                    current_source = os.stat(
                        source_name,
                        dir_fd=source_parent_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISREG(current_source.st_mode)
                        or self._source_from_stat(current_source) != context.source
                    ):
                        raise WorkspacePathError("The approved source file identity has changed.")
                    result = self._renameat2(
                        source_parent_fd,
                        os.fsencode(source_name),
                        destination_parent_fd,
                        os.fsencode(destination_name),
                        self._RENAME_NOREPLACE,
                    )
                    if result != 0:
                        error_number = ctypes.get_errno()
                        if error_number == errno.EEXIST:
                            raise FileExistsError("The destination already exists.")
                        if error_number in {
                            errno.ENOSYS,
                            errno.EINVAL,
                            errno.EOPNOTSUPP,
                            errno.EXDEV,
                        }:
                            raise WorkspacePathError(
                                "Atomic no-overwrite moves are unsupported on this filesystem."
                            )
                        raise WorkspacePathError("The requested move could not be completed.")
        finally:
            os.close(source_fd)


if os.name == "nt":
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = cast(int, ctypes.c_void_p(-1).value)
    _GENERIC_READ = 0x80000000
    _DELETE = 0x00010000
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
    _FILE_ID_INFO_CLASS = 18
    _FILE_RENAME_INFO_CLASS = 3

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    class _FileTime(ctypes.Structure):
        _fields_ = [
            ("Low", wintypes.DWORD),
            ("High", wintypes.DWORD),
        ]

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("CreationTime", _FileTime),
            ("LastAccessTime", _FileTime),
            ("LastWriteTime", _FileTime),
            ("VolumeSerialNumber", wintypes.DWORD),
            ("FileSizeHigh", wintypes.DWORD),
            ("FileSizeLow", wintypes.DWORD),
            ("NumberOfLinks", wintypes.DWORD),
            ("FileIndexHigh", wintypes.DWORD),
            ("FileIndexLow", wintypes.DWORD),
        ]

    class _FileRenameInfoHeader(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
        ]

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]


class _WindowsWorkspaceBackend:
    def __init__(self, workspace_root: Path) -> None:
        if os.name != "nt":
            raise WorkspacePathError("Secure Windows workspace access is unavailable.")
        self.root = workspace_root.absolute()
        self.workspace_name = self.root.name
        win_dll = cast(Any, getattr(ctypes, "WinDLL"))  # noqa: B009
        self._kernel32 = win_dll("kernel32", use_last_error=True)
        self._configure_functions()
        self._root_handle = self._open_path(
            self.root,
            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
            directory=True,
        )
        try:
            self._require_safe_kind(self._root_handle, directory=True)
            self._selected_identity = self._identity(self._root_handle)
            self._selected_final_path = self._final_path(self._root_handle)
            configured_path = self._normalize_final_path(str(self.root))
            if configured_path != self._selected_final_path:
                raise WorkspacePathError(
                    "Reparse points are not allowed in the configured workspace path."
                )
        except Exception:
            self._close(self._root_handle)
            raise

    def __del__(self) -> None:
        handle = getattr(self, "_root_handle", None)
        if isinstance(handle, int) and handle != _INVALID_HANDLE_VALUE:
            self._close(handle)
            self._root_handle = _INVALID_HANDLE_VALUE

    def _configure_functions(self) -> None:
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        self._kernel32.ReadFile.restype = wintypes.BOOL
        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL

    def _open_path(self, path: Path, access: int, *, directory: bool) -> int:
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        handle = self._kernel32.CreateFileW(
            str(path),
            access,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise WorkspacePathError("The requested workspace path is unavailable.")
        return cast(int, handle)

    def _close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)

    def _attributes(self, handle: int) -> int:
        info = _FileAttributeTagInfo()
        if not self._kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ATTRIBUTE_TAG_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise WorkspacePathError("The requested workspace path is unavailable.")
        return int(info.FileAttributes)

    def _require_safe_kind(self, handle: int, *, directory: bool) -> None:
        attributes = self._attributes(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise WorkspacePathError("Windows reparse points are not allowed.")
        is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if is_directory != directory:
            raise WorkspacePathError("The requested workspace object has an invalid type.")

    def _identity(self, handle: int) -> FilesystemObjectIdentity:
        info = _FileIdInfo()
        if not self._kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise WorkspacePathError("Filesystem identity is unavailable.")
        object_id = bytes(info.FileId.Identifier).hex()
        return FilesystemObjectIdentity(
            platform="windows",
            volume_id=f"{info.VolumeSerialNumber:016x}",
            object_id=object_id,
        )

    def _source_precondition(self, handle: int) -> SourceFilePrecondition:
        info = _ByHandleFileInformation()
        if not self._kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise WorkspacePathError("Filesystem identity is unavailable.")
        size = (int(info.FileSizeHigh) << 32) | int(info.FileSizeLow)
        modified = (int(info.LastWriteTime.High) << 32) | int(info.LastWriteTime.Low)
        created = (int(info.CreationTime.High) << 32) | int(info.CreationTime.Low)
        return SourceFilePrecondition(
            identity=self._identity(handle),
            size_bytes=size,
            modified_ns=modified * 100,
            changed_ns=created * 100,
        )

    def _final_path(self, handle: int) -> str:
        required = self._kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if required == 0:
            raise WorkspacePathError("The requested workspace path is unavailable.")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = self._kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise WorkspacePathError("The requested workspace path is unavailable.")
        return self._normalize_final_path(buffer.value)

    @staticmethod
    def _normalize_final_path(value: str) -> str:
        normalized = os.path.normcase(os.path.abspath(value).rstrip("\\"))
        if normalized.startswith("\\\\?\\unc\\"):
            return f"\\\\{normalized[8:]}"
        if normalized.startswith("\\\\?\\"):
            return normalized[4:]
        return normalized

    def _require_contained_handle(self, handle: int) -> None:
        final_path = self._final_path(handle)
        prefix = f"{self._selected_final_path}\\"
        if final_path != self._selected_final_path and not final_path.startswith(prefix):
            raise WorkspacePathError("The requested path is outside the workspace.")

    @contextmanager
    def _open_chain(
        self,
        relative_path: str,
        *,
        final_access: int,
        final_directory: bool,
    ) -> Iterator[int]:
        handles: list[int] = []
        current = self.root
        parts = PurePosixPath(relative_path).parts if relative_path != "." else ()
        try:
            if not parts:
                handle = self._open_path(
                    current,
                    final_access,
                    directory=final_directory,
                )
                handles.append(handle)
                self._require_safe_kind(handle, directory=final_directory)
                self._require_contained_handle(handle)
                yield handle
                return

            for index, component in enumerate(parts):
                current /= component
                is_final = index == len(parts) - 1
                directory = final_directory if is_final else True
                access = final_access if is_final else _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES
                handle = self._open_path(current, access, directory=directory)
                handles.append(handle)
                self._require_safe_kind(handle, directory=directory)
                self._require_contained_handle(handle)
            yield handles[-1]
        finally:
            for handle in reversed(handles):
                self._close(handle)

    def assert_workspace_identity(self, expected: FilesystemObjectIdentity | None = None) -> None:
        current = self._open_path(
            self.root,
            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
            directory=True,
        )
        try:
            self._require_safe_kind(current, directory=True)
            identity = self._identity(current)
        finally:
            self._close(current)
        if identity != self._selected_identity or (expected is not None and identity != expected):
            raise WorkspacePathError("The configured workspace identity has changed.")

    def approval_context(self, relative_source: str) -> MoveApprovalContext:
        self.assert_workspace_identity()
        with self._open_chain(
            relative_source,
            final_access=_FILE_READ_ATTRIBUTES,
            final_directory=False,
        ) as source_handle:
            source = self._source_precondition(source_handle)
        return MoveApprovalContext(workspace=self._selected_identity, source=source)

    def directory_batch(self, relative_path: str, visit_budget: int) -> DirectoryBatch:
        self.assert_workspace_identity()
        names: list[str] = []
        visited = 0
        truncated = False
        with self._open_chain(
            relative_path,
            final_access=_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
            final_directory=True,
        ):
            try:
                with os.scandir(
                    self.root
                    if relative_path == "."
                    else self.root.joinpath(*PurePosixPath(relative_path).parts)
                ) as entries:
                    for entry in entries:
                        visited += 1
                        if visited == visit_budget:
                            truncated = True
                            break
                        names.append(entry.name)
            except OSError as exc:
                raise WorkspacePathError("The requested directory is unavailable.") from exc
        names.sort(key=str.casefold)
        return DirectoryBatch(tuple(names), visited, truncated)

    def entry_kind(self, relative_path: str) -> Literal["directory", "file"] | None:
        try:
            with self._open_chain(
                relative_path,
                final_access=_FILE_READ_ATTRIBUTES,
                final_directory=False,
            ):
                return "file"
        except WorkspacePathError:
            try:
                with self._open_chain(
                    relative_path,
                    final_access=_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES,
                    final_directory=True,
                ):
                    return "directory"
            except WorkspacePathError:
                return None

    def read_regular_file(self, relative_path: str, max_bytes: int) -> bytes:
        self.assert_workspace_identity()
        with self._open_chain(
            relative_path,
            final_access=_GENERIC_READ | _FILE_READ_ATTRIBUTES,
            final_directory=False,
        ) as handle:
            buffer = ctypes.create_string_buffer(max_bytes + 1)
            bytes_read = wintypes.DWORD()
            if not self._kernel32.ReadFile(
                handle,
                buffer,
                max_bytes + 1,
                ctypes.byref(bytes_read),
                None,
            ):
                raise WorkspacePathError("The requested file is unavailable or unreadable.")
            payload = buffer.raw[: bytes_read.value]
        if len(payload) > max_bytes:
            raise WorkspaceFileTooLarge("The requested file exceeds the read limit.")
        return payload

    def move_no_replace(
        self,
        source: str,
        destination: str,
        context: MoveApprovalContext,
    ) -> None:
        self.assert_workspace_identity(context.workspace)
        destination_path = PurePosixPath(destination)
        destination_parent = destination_path.parent.as_posix()
        with self._open_chain(
            source,
            final_access=_DELETE | _FILE_READ_ATTRIBUTES,
            final_directory=False,
        ) as source_handle:
            if self._source_precondition(source_handle) != context.source:
                raise WorkspacePathError("The approved source file identity has changed.")
            with self._open_chain(
                destination_parent,
                final_access=_FILE_TRAVERSE | _FILE_READ_ATTRIBUTES,
                final_directory=True,
            ) as _destination_parent_handle:
                destination_full_path = self.root.joinpath(*destination_path.parts)
                encoded_name = str(destination_full_path).encode("utf-16-le")
                filename_offset = _FileRenameInfo.FileName.offset
                buffer = ctypes.create_string_buffer(
                    ctypes.sizeof(_FileRenameInfo) + len(encoded_name)
                )
                header = _FileRenameInfoHeader.from_buffer(buffer)
                header.Flags = 0
                header.RootDirectory = None
                header.FileNameLength = len(encoded_name)
                ctypes.memmove(
                    ctypes.addressof(buffer) + filename_offset,
                    encoded_name,
                    len(encoded_name),
                )
                if not self._kernel32.SetFileInformationByHandle(
                    source_handle,
                    _FILE_RENAME_INFO_CLASS,
                    buffer,
                    len(buffer),
                ):
                    error_number = cast(
                        int,
                        getattr(ctypes, "get_last_error")(),  # noqa: B009
                    )
                    if error_number in {80, 183}:
                        raise FileExistsError("The destination already exists.")
                    if error_number in {1, 17, 50, 87}:
                        raise WorkspacePathError(
                            "Atomic no-overwrite moves are unsupported on this filesystem."
                        )
                    windows_error = cast(
                        OSError,
                        getattr(ctypes, "WinError")(error_number),  # noqa: B009
                    )
                    raise WorkspacePathError(
                        "The requested move could not be completed."
                    ) from windows_error


def serialized_item_size(item: dict[str, Any]) -> int:
    """Return the exact compact UTF-8 size used for result-budget accounting."""

    return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
