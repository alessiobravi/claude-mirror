"""StorageBackend implementation for SMB/CIFS shares.

Targets SMB2/3 only. SMBv1 is intentionally NOT supported — the protocol
is end-of-life on every modern OS and re-introducing it would re-open
EternalBlue-class attack surface. The doctor's protocol-negotiation
deep check rejects v1-only servers explicitly.

Layout mirrors `SFTPBackend`: path-as-id, no OAuth dance, credentials
stored inline in the YAML at chmod 0600. Uploads use a `.tmp` file +
rename so a crashed transfer cannot leave a truncated file at the
destination path. SMB has no native server-side hash primitive; hashes
are computed client-side by streaming the file (matches SFTP without
shell-level `sha256sum`).

`smbprotocol` is lazy-imported per the v0.5.61 fusepy precedent so a
`claude-mirror doctor` against a non-SMB backend never pays the import
cost.
"""
from __future__ import annotations

import errno
import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure


_DOWNLOAD_CHUNK = 64 * 1024
_UPLOAD_CHUNK = 64 * 1024
# 50 MiB cutoff for `copy_file`'s in-memory fallback. Anything bigger
# round-trips through a temp file rather than buffering the whole blob
# in RAM during the download-then-upload path.
_COPY_MEMORY_BUDGET = 50 * 1024 * 1024


def _smbclient() -> Any:
    """Lazy-import `smbclient`. Importing at call sites would force a
    circular dance with `smbprotocol`'s exceptions module; importing at
    module top would punish every non-SMB backend with the load cost."""
    import smbclient  # noqa: PLC0415
    return smbclient


def _smbexc() -> Any:
    import smbprotocol.exceptions  # noqa: PLC0415
    return smbprotocol.exceptions


class SmbBackend(StorageBackend):
    """StorageBackend implementation for SMB/CIFS shares (SMB2/3 only)."""

    backend_name = "smb"

    def __init__(self, config: Config) -> None:
        self.config = config
        # `smbclient` keeps a process-wide session registry keyed on
        # server name. We track whether THIS backend instance has
        # registered its session so authenticate() / get_credentials()
        # can short-circuit safely under repeated calls.
        self._session_registered = False
        # Last-known negotiated encryption state (set by authenticate);
        # informational only. The wire-level negotiation happens inside
        # smbprotocol's Connection object.
        self._encryption_negotiated: Optional[bool] = None

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _share_root(self) -> str:
        """UNC root of the configured share: `\\\\server\\share`."""
        return f"\\\\{self.config.smb_server}\\{self.config.smb_share}"

    def _to_unc(self, rel: str) -> str:
        """Join a forward-slash relative path onto the share root.

        SMB UNC paths use backslashes; callers anywhere in the codebase
        pass forward-slash paths (the cross-backend convention). The
        `smbclient` package accepts both, but we normalise to the canonical
        backslash form for stable file-id semantics — `get_file_id` returns
        the same string the engine compares against later.
        """
        rel = rel.replace("/", "\\").lstrip("\\")
        if not rel:
            return self._share_root()
        return f"{self._share_root()}\\{rel}"

    def _project_root(self) -> str:
        """UNC path to the configured project folder under the share."""
        folder = (self.config.smb_folder or "").strip()
        if not folder:
            return self._share_root()
        return self._to_unc(folder)

    @staticmethod
    def _join(parent: str, name: str) -> str:
        """Append `name` under a UNC `parent`. Both are kept in
        backslash form so the result round-trips through `get_file_id`."""
        return f"{parent.rstrip(chr(92))}\\{name}"

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _ensure_session(self) -> None:
        """Register the smbclient session if we haven't already.

        `smbclient.register_session` is idempotent at the module level —
        re-registering against the same `server` is a no-op — so this
        guard is mainly to skip the import cost on hot paths.
        """
        if self._session_registered:
            return
        smbclient = _smbclient()
        kwargs: dict[str, Any] = {
            "username": self.config.smb_username or None,
            "password": self.config.smb_password or None,
            "port": int(self.config.smb_port or 445),
            "encrypt": bool(self.config.smb_encryption),
        }
        # `smbprotocol` reads `smb_domain` from the username when it
        # contains a backslash (`DOMAIN\\user`). When the YAML supplies
        # both fields separately, fold them together so the SDK sees the
        # canonical NTLM form.
        domain = (self.config.smb_domain or "").strip()
        if domain and kwargs["username"] and "\\" not in str(kwargs["username"]):
            kwargs["username"] = f"{domain}\\{kwargs['username']}"
        smbclient.register_session(self.config.smb_server, **kwargs)
        self._session_registered = True
        self._encryption_negotiated = bool(self.config.smb_encryption)

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw smbprotocol / network / IO exception to ErrorClass.

        Matched by class NAME rather than `isinstance` against imported
        types so the classifier doesn't pay the smbprotocol import cost
        on the generic error path. Names cover smbprotocol 1.13+
        (`LogonFailure`, `AccessDenied`, `ObjectNameNotFound`,
        `BadNetworkName`, `DiskFull`). Account-name and network-class
        flavours fall through to the substring match below.
        """
        try:
            exc_module = type(exc).__module__ or ""
            exc_name = type(exc).__name__
            if exc_module.startswith("smbprotocol"):
                if exc_name in ("LogonFailure", "BadAccountName"):
                    return ErrorClass.AUTH
                if exc_name == "AccessDenied":
                    return ErrorClass.PERMISSION
                if exc_name in ("ObjectNameNotFound", "BadNetworkName"):
                    return ErrorClass.FILE_REJECTED
                if exc_name in ("DiskFull", "OutOfPaperOrUnknownStorage"):
                    return ErrorClass.QUOTA
                if "Network" in exc_name:
                    return ErrorClass.TRANSIENT
                msg = str(exc).lower()
                if "logon" in msg or "bad account" in msg:
                    return ErrorClass.AUTH
                if "access denied" in msg:
                    return ErrorClass.PERMISSION
                if "disk full" in msg or "quota" in msg or "no space" in msg:
                    return ErrorClass.QUOTA
                if "not found" in msg or "no such" in msg:
                    return ErrorClass.FILE_REJECTED
            if isinstance(exc, (socket.timeout, ConnectionResetError, ConnectionRefusedError)):
                return ErrorClass.TRANSIENT
            if isinstance(exc, IOError):
                msg = str(exc).lower()
                if "quota" in msg or "disk full" in msg or "no space" in msg:
                    return ErrorClass.QUOTA
            return ErrorClass.UNKNOWN
        except Exception:
            return ErrorClass.UNKNOWN

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Open a session, ensure the project folder exists, persist a
        non-secret 'verified at' marker to token_file."""
        smbclient = _smbclient()
        self._ensure_session()
        smbclient.makedirs(self._project_root(), exist_ok=True)

        token_path = Path(self.config.token_file)
        write_token_secure(token_path, json.dumps({
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "server": self.config.smb_server,
            "share": self.config.smb_share,
            "encryption_requested": bool(self.config.smb_encryption),
        }))

    def get_credentials(self) -> Any:
        """Return a registered smbclient module proxy.

        SMB has no per-call credential object — auth state lives in
        smbclient's process-wide session registry. We return the module
        itself so callers that want to make further calls can use the
        same idiomatic surface as the rest of the backend.
        """
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            raise RuntimeError(
                "SMB not authenticated — run claude-mirror auth"
            )
        self._ensure_session()
        return _smbclient()

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Create `name` under `parent_id` if absent; return its UNC path."""
        smbclient = _smbclient()
        path = self._join(parent_id, name)
        smbclient.makedirs(path, exist_ok=True)
        return path

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Walk `rel_path` components, mkdir-ing intermediates. Returns
        (parent_path, basename)."""
        # Use forward-slash split — matches the cross-backend convention.
        parts = [p for p in Path(rel_path).parts if p not in ("", ".", "/")]
        if len(parts) <= 1:
            return root_folder_id, parts[0] if parts else ""
        current = root_folder_id
        for part in parts[:-1]:
            current = self.get_or_create_folder(part, current)
        return current, parts[-1]

    # ------------------------------------------------------------------
    # File listing
    # ------------------------------------------------------------------

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Recursive walk via `smbclient.scandir`. md5 is None — SMB has
        no native checksum, so the engine falls back to size+mtime+local-hash."""
        smbclient = _smbclient()
        excluded = exclude_folder_names or set()
        results: list[dict[str, Any]] = []
        folders_done = 0
        files_seen = 0

        def _walk(path: str, rel_prefix: str) -> None:
            nonlocal folders_done, files_seen
            try:
                entries = list(smbclient.scandir(path))
            except OSError:
                return
            folders_done += 1
            for entry in entries:
                name = entry.name
                if not name or name in (".", ".."):
                    continue
                child_path = self._join(path, name)
                child_rel = f"{rel_prefix}{name}" if rel_prefix else name
                if entry.is_dir():
                    if name in excluded:
                        continue
                    _walk(child_path, f"{child_rel}/")
                else:
                    try:
                        st = entry.stat()
                        size = int(getattr(st, "st_size", 0) or 0)
                    except OSError:
                        size = 0
                    results.append({
                        "id": child_path,
                        "name": name,
                        "relative_path": child_rel,
                        "size": size,
                        "md5Checksum": None,
                    })
                    files_seen += 1
                    if progress_cb and (files_seen % 50 == 0):
                        progress_cb(folders_done, files_seen)

        _walk(folder_id, prefix)
        if progress_cb:
            progress_cb(folders_done, files_seen)
        return results

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict[str, Any]]:
        """List subfolders. If `name` given, filter to exact match."""
        smbclient = _smbclient()
        results: list[dict[str, Any]] = []
        try:
            entries = list(smbclient.scandir(parent_id))
        except OSError:
            return results
        for entry in entries:
            if not entry.is_dir():
                continue
            fname = entry.name
            if not fname or fname in (".", ".."):
                continue
            if name is not None and fname != name:
                continue
            try:
                st = entry.stat()
                mtime = getattr(st, "st_mtime", None)
            except OSError:
                mtime = None
            try:
                created = (
                    datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
                    if mtime is not None
                    else ""
                )
            except (OverflowError, OSError, ValueError):
                created = ""
            results.append({
                "id": self._join(parent_id, fname),
                "name": fname,
                "createdTime": created,
            })
        return results

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Upload via atomic .tmp + replace.

        SMB's POSIX-rename equivalent is `smbclient.replace`, which maps
        to the underlying `SetInfoRequest` with `FileRenameInformation`
        + `replace_if_exists=True`. Mirrors the SFTP atomicity contract:
        the destination is never observed mid-write.
        """
        smbclient = _smbclient()
        if file_id:
            dst = file_id
        else:
            parent, basename = self.resolve_path(rel_path, root_folder_id)
            dst = self._join(parent, basename)

        tmp = f"{dst}.tmp"
        bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))
        try:
            with open(local_path, "rb") as src, smbclient.open_file(tmp, mode="wb") as dst_f:
                while True:
                    block = src.read(_UPLOAD_CHUNK)
                    if not block:
                        break
                    bucket.consume(len(block))
                    dst_f.write(block)
                    if progress_callback is not None:
                        progress_callback(len(block))
            self._replace(tmp, dst)
        except Exception:
            try:
                smbclient.remove(tmp)
            except Exception:
                pass
            raise
        return dst

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        """Atomic write of `content` as `name` under `folder_id`. mimetype
        is ignored — SMB has no MIME concept."""
        smbclient = _smbclient()
        if file_id:
            dst = file_id
        else:
            dst = self._join(folder_id, name)

        tmp = f"{dst}.tmp"
        try:
            with smbclient.open_file(tmp, mode="wb") as f:
                f.write(content)
            self._replace(tmp, dst)
        except Exception:
            try:
                smbclient.remove(tmp)
            except Exception:
                pass
            raise
        return dst

    def _replace(self, src: str, dst: str) -> None:
        """Atomic rename src -> dst, replacing dst if it exists.

        smbclient's `replace` was added in 1.10; on older builds we fall
        back to `remove` + `rename`, which is racy but matches the SFTP
        precedent for servers without `posix_rename`.
        """
        smbclient = _smbclient()
        replace = getattr(smbclient, "replace", None)
        if callable(replace):
            replace(src, dst)
            return
        try:
            smbclient.remove(dst)
        except Exception:
            pass
        smbclient.rename(src, dst)

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bytes:
        """Stream-download in 64 KiB chunks, aborting past MAX_DOWNLOAD_BYTES."""
        smbclient = _smbclient()
        buf = bytearray()
        with smbclient.open_file(file_id, mode="rb") as f:
            while True:
                chunk = f.read(_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > self.MAX_DOWNLOAD_BYTES:
                    raise BackendError(
                        ErrorClass.FILE_REJECTED,
                        f"SMB download of {file_id!r} streamed past "
                        f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting.",
                        backend_name="smb",
                    )
                if progress_callback is not None:
                    progress_callback(len(chunk))
        return bytes(buf)

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Return the UNC path if `name` exists in `folder_id`, else None.

        SMB has no separate file-id concept; the path IS the identifier.
        """
        smbclient = _smbclient()
        path = self._join(folder_id, name)
        try:
            smbclient.stat(path)
        except OSError:
            return None
        return path

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Download-then-upload fallback (no server-side copy in smbclient).

        SMB2/3 supports server-side copy via the FSCTL_SRV_COPYCHUNK ioctl,
        but `smbclient` does not expose it as a Python API and writing the
        ioctl by hand against `smbprotocol.open.Open` would double the
        backend's surface area for a corner-case optimisation. We fall back
        to a streaming download + upload via temp file when the source is
        bigger than `_COPY_MEMORY_BUDGET` (50 MiB) so a large copy doesn't
        OOM the client; smaller copies stay in memory for speed.
        """
        smbclient = _smbclient()
        dst = self._join(dest_folder_id, name)
        try:
            st = smbclient.stat(source_file_id)
            size = int(getattr(st, "st_size", 0) or 0)
        except OSError:
            size = 0

        if size <= _COPY_MEMORY_BUDGET:
            content = self.download_file(source_file_id)
            return self.upload_bytes(content, name, dest_folder_id)

        tmp = f"{dst}.tmp"
        try:
            with smbclient.open_file(source_file_id, mode="rb") as src, \
                    smbclient.open_file(tmp, mode="wb") as out:
                while True:
                    chunk = src.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
            self._replace(tmp, dst)
        except Exception:
            try:
                smbclient.remove(tmp)
            except Exception:
                pass
            raise
        return dst

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Stream the file content + compute sha256 client-side.

        SMB has no in-protocol checksum (the SMB2 ChannelInfo / signing
        digests aren't content hashes). Returning None here would push the
        engine onto a size+mtime fallback, which is sufficient on a
        single-machine sync but flaky across machines with clock skew —
        so we pay one extra read pass and produce a real digest.
        """
        smbclient = _smbclient()
        try:
            h = hashlib.sha256()
            with smbclient.open_file(file_id, mode="rb") as f:
                while True:
                    chunk = f.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def delete_file(self, file_id: str) -> None:
        smbclient = _smbclient()
        try:
            smbclient.remove(file_id)
        except OSError as e:
            # Fall through to rmdir if the entry is a directory; matches
            # the SFTP backend's symmetric behaviour for safety on stale
            # path-as-id values that point at a folder.
            code = getattr(e, "errno", None)
            msg = str(e).lower()
            if code == errno.EISDIR or "is a directory" in msg or "directory" in msg:
                smbclient.rmdir(file_id)
                return
            raise
