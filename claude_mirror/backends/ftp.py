"""StorageBackend implementation for FTP and FTPS servers.

Targets the legacy shared-hosting market (cPanel / DirectAdmin / old
WordPress hosts) and NAS-on-LAN setups. Uses Python's stdlib
``ftplib`` only — no third-party dependency on top of the existing
base install.

Three TLS modes:

  * ``explicit`` (default) — `ftplib.FTP_TLS` with `auth()` after the
    server greeting; standard FTPS-over-21 negotiation as defined in
    RFC 4217. The data channel is also encrypted via `prot_p()`.
  * ``implicit`` — legacy FTPS-on-990 where the control channel is
    inside TLS from the first byte. ``FTP_TLS`` does not implement
    implicit-mode wrapping out of the box, so the backend wraps the
    socket with ``ssl.SSLContext.wrap_socket`` before handing it to
    ftplib.
  * ``off`` — plain FTP. Credentials AND payloads cross the wire in
    cleartext. ``authenticate()`` emits a loud warning every time it
    runs; ``_run_ftp_deep_checks`` adds a doctor-time hint when the
    configured host isn't loopback / RFC1918.

Layout mirrors SFTPBackend (path-as-id, no native push). FTP has no
server-side copy, no native checksum command in the base spec, and no
notification channel — every operation degrades to a client-side
implementation, which is the realistic posture for shared-hosting
servers.
"""
from __future__ import annotations

import ftplib
import hashlib
import io
import ipaddress
import json
import os
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure


_DOWNLOAD_CHUNK = 64 * 1024
_UPLOAD_CHUNK = 32 * 1024


CLEARTEXT_WARNING = (
    "⚠ FTP cleartext mode enabled — username + password "
    "travel UNENCRYPTED on every connection.\n"
    "  This is appropriate ONLY for trusted local-network use "
    "(e.g. NAS on a LAN).\n"
    "  For internet-facing servers, switch to ftp_tls: explicit "
    "(FTPS) or use the SFTP backend."
)


def _emit_cleartext_warning(host: str) -> None:
    """Stderr-print the cleartext-FTP warning once per call site.

    We deliberately print to stderr (not via the rich console used in
    cli.py) so the backend stays library-shaped — callers that import
    ftp.py outside the CLI still see the warning. The host is included
    so users tailing logs across many configs can tell which server
    is the offender.
    """
    print(f"{CLEARTEXT_WARNING}\n  host: {host}", file=sys.stderr, flush=True)


def _is_loopback_or_rfc1918(host: str) -> bool:
    """True if ``host`` resolves to loopback or RFC1918 private space.

    Used by the doctor's cleartext-mode advisory: cleartext FTP against
    a public IP is a loud failure; against 10/8, 172.16/12, 192.168/16,
    or 127/8 it's only an advisory line.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            addr = ipaddress.ip_address(resolved)
        except (socket.gaierror, ValueError, OSError):
            return False
    return addr.is_loopback or addr.is_private


class FtpBackend(StorageBackend):
    """StorageBackend implementation for FTP / FTPS servers."""

    backend_name = "ftp"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._ftp: Optional[ftplib.FTP] = None

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _open_connection(self) -> ftplib.FTP:
        """Open the right ftplib client based on ``ftp_tls`` mode.

        Implicit FTPS isn't natively supported by `ftplib.FTP_TLS`
        (which expects to AUTH-upgrade after the server greeting), so
        we wrap the socket ourselves with `ssl.SSLContext.wrap_socket`
        before passing it through.
        """
        host = self.config.ftp_host
        port = int(self.config.ftp_port or 21)
        tls_mode = (self.config.ftp_tls or "explicit").lower()
        username = self.config.ftp_username or "anonymous"
        password = self.config.ftp_password or ""
        passive = bool(self.config.ftp_passive)

        if tls_mode == "off":
            _emit_cleartext_warning(host)
            ftp: ftplib.FTP = ftplib.FTP()
            ftp.connect(host=host, port=port, timeout=30)
            ftp.login(user=username, passwd=password)
        elif tls_mode == "explicit":
            tls = ftplib.FTP_TLS()
            tls.connect(host=host, port=port, timeout=30)
            tls.auth()
            tls.login(user=username, passwd=password)
            tls.prot_p()
            ftp = tls
        elif tls_mode == "implicit":
            ctx = ssl.create_default_context()
            sock = socket.create_connection((host, port), timeout=30)
            wrapped = ctx.wrap_socket(sock, server_hostname=host)
            tls = ftplib.FTP_TLS()
            tls.sock = wrapped
            try:
                file_obj = wrapped.makefile("r", encoding="latin-1")
            except TypeError:
                file_obj = wrapped.makefile("r")
            tls.file = file_obj
            tls.welcome = tls.getresp()
            tls.login(user=username, passwd=password)
            tls.prot_p()
            ftp = tls
        else:
            raise BackendError(
                ErrorClass.UNKNOWN,
                f"Unsupported ftp_tls mode: {tls_mode!r} "
                f"(expected 'off', 'explicit', or 'implicit')",
                backend_name="ftp",
            )

        ftp.set_pasv(passive)
        return ftp

    def _connect(self) -> ftplib.FTP:
        """Lazy-open + cache the FTP/FTPS client. Reconnect when the
        cached client looks closed (a torn-down socket or a server-
        forced timeout)."""
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except (ftplib.error_temp, ftplib.error_perm,
                    ftplib.error_proto, ftplib.error_reply,
                    OSError, EOFError):
                try:
                    self._ftp.close()
                except Exception:
                    pass
                self._ftp = None

        self._ftp = self._open_connection()
        return self._ftp

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _join(parent: str, name: str) -> str:
        if not parent:
            return name
        return f"{parent.rstrip('/')}/{name}"

    def _mkdir_p(self, ftp: ftplib.FTP, path: str) -> None:
        """Recursively `mkd` ``path``, ignoring already-exists 550s."""
        if not path or path == "/":
            return
        parts = [p for p in path.split("/") if p]
        anchor = "/" if path.startswith("/") else ""
        current = anchor
        for part in parts:
            current = f"{current.rstrip('/')}/{part}" if current else part
            try:
                ftp.mkd(current)
            except ftplib.error_perm as e:
                msg = str(e).lower()
                if "exists" in msg or "550" in msg:
                    continue
                raise

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw ftplib / socket / ssl exception to an ErrorClass.

        ftplib raises numbered subclasses of `error_reply` whose message
        is the server's RFC 959 response code + text. We branch on the
        leading 3-digit code to get stable behaviour across servers
        that differ in their textual phrasing.
        """
        try:
            if isinstance(exc, ssl.SSLError):
                return ErrorClass.AUTH
            if isinstance(exc, ftplib.error_perm):
                msg = str(exc)
                code = msg[:3] if len(msg) >= 3 and msg[:3].isdigit() else ""
                lower = msg.lower()
                if code == "530":
                    return ErrorClass.AUTH
                if code == "552":
                    return ErrorClass.QUOTA
                if code == "550":
                    if "permission" in lower or "denied" in lower:
                        return ErrorClass.PERMISSION
                    if "no such" in lower or "not found" in lower:
                        return ErrorClass.FILE_REJECTED
                    if "exists" in lower:
                        return ErrorClass.FILE_REJECTED
                    return ErrorClass.FILE_REJECTED
                if "quota" in lower or "no space" in lower or "disk full" in lower:
                    return ErrorClass.QUOTA
                return ErrorClass.UNKNOWN
            if isinstance(exc, ftplib.error_temp):
                return ErrorClass.TRANSIENT
            if isinstance(exc, (socket.timeout, ConnectionResetError,
                                ConnectionRefusedError, BrokenPipeError,
                                EOFError, ftplib.error_proto,
                                ftplib.error_reply)):
                return ErrorClass.TRANSIENT
            return ErrorClass.UNKNOWN
        except Exception:
            return ErrorClass.UNKNOWN

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Open a connection, ensure the project folder exists, and
        write a non-secret 'verified at' marker to ``token_file``."""
        ftp = self._connect()
        folder = self.config.ftp_folder or ""
        if folder:
            try:
                ftp.cwd(folder)
            except ftplib.error_perm:
                self._mkdir_p(ftp, folder)
                ftp.cwd(folder)

        token_path = Path(self.config.token_file)
        write_token_secure(token_path, json.dumps({
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "host": self.config.ftp_host,
            "tls": self.config.ftp_tls,
        }))

    def get_credentials(self) -> ftplib.FTP:
        """Return a connected FTP client. Raises if auth hasn't run."""
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            raise RuntimeError(
                "FTP not authenticated — run claude-mirror auth"
            )
        return self._connect()

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Create ``name`` under ``parent_id`` if absent; return its
        full path.

        FTP ``mkd`` returns 550 on EXIST; we suppress the 550 only
        when the message indicates an exists condition rather than a
        permission denied or other 550 variant.
        """
        path = self._join(parent_id, name)
        ftp = self._connect()
        try:
            ftp.mkd(path)
        except ftplib.error_perm as e:
            msg = str(e).lower()
            if "exists" not in msg and "550" not in msg:
                raise
        return path

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Walk ``rel_path`` components, mkdir-ing intermediates."""
        parts = [p for p in PurePosixPath(rel_path).parts if p not in ("", ".", "/")]
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
        """Recursive walk via ``MLSD`` (RFC 3659; modern, structured)
        with a ``LIST`` (RFC 959; legacy, free-form) fallback.

        Some shared-hosting servers don't expose MLSD — we detect that
        via the 502/500 series and fall through to a best-effort LIST
        parse. LIST output is server-dependent; we recognise the two
        most common shapes (Unix-style and IIS).
        """
        excluded = exclude_folder_names or set()
        results: list[dict[str, Any]] = []
        folders_done = 0
        files_seen = 0
        ftp = self._connect()

        def _walk(path: str, rel_prefix: str) -> None:
            nonlocal folders_done, files_seen
            entries = list(_iter_entries(ftp, path))
            folders_done += 1
            for name, kind, size in entries:
                if not name or name in (".", ".."):
                    continue
                child_path = self._join(path, name)
                child_rel = f"{rel_prefix}{name}" if rel_prefix else name
                if kind == "dir":
                    if name in excluded:
                        continue
                    _walk(child_path, f"{child_rel}/")
                else:
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

    def list_folders(
        self,
        parent_id: str,
        name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List subfolders of ``parent_id``. Filter by ``name`` if set."""
        ftp = self._connect()
        results: list[dict[str, Any]] = []
        try:
            entries = list(_iter_entries(ftp, parent_id))
        except ftplib.error_perm:
            return results
        for entry_name, kind, _size in entries:
            if kind != "dir":
                continue
            if not entry_name or entry_name in (".", ".."):
                continue
            if name is not None and entry_name != name:
                continue
            results.append({
                "id": self._join(parent_id, entry_name),
                "name": entry_name,
                "createdTime": "",
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
        """Upload via ``STOR``. We do NOT do a `.tmp` + rename dance:
        many shared-hosting FTP servers don't implement RNFR/RNTO
        atomically (or at all), and the SFTP-style POSIX rename
        guarantee doesn't exist on raw FTP. Callers that need
        atomicity should prefer SFTP or WebDAV.
        """
        if file_id:
            dst = file_id
        else:
            parent, basename = self.resolve_path(rel_path, root_folder_id)
            dst = self._join(parent, basename)

        ftp = self._connect()
        bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))

        def _on_block(block: bytes) -> None:
            bucket.consume(len(block))
            if progress_callback is not None:
                progress_callback(len(block))

        with open(local_path, "rb") as src:
            ftp.storbinary(
                f"STOR {dst}", src,
                blocksize=_UPLOAD_CHUNK,
                callback=_on_block,
            )
        return dst

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        """Upload ``content`` as ``name`` under ``folder_id``."""
        if file_id:
            dst = file_id
        else:
            dst = self._join(folder_id, name)
        ftp = self._connect()
        ftp.storbinary(f"STOR {dst}", io.BytesIO(content), blocksize=_UPLOAD_CHUNK)
        return dst

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
        max_bytes: Optional[int] = None,
    ) -> bytes:
        """Download ``file_id`` via ``RETR``. Caps at
        ``MAX_DOWNLOAD_BYTES`` (or ``max_bytes`` when provided)."""
        cap = max_bytes if max_bytes is not None else self.MAX_DOWNLOAD_BYTES
        ftp = self._connect()
        buf = bytearray()
        aborted: dict[str, bool] = {"hit": False}

        def _on_block(block: bytes) -> None:
            if aborted["hit"]:
                return
            buf.extend(block)
            if len(buf) > cap:
                aborted["hit"] = True
                return
            if progress_callback is not None:
                progress_callback(len(block))

        ftp.retrbinary(f"RETR {file_id}", _on_block, blocksize=_DOWNLOAD_CHUNK)
        if aborted["hit"]:
            raise BackendError(
                ErrorClass.FILE_REJECTED,
                f"FTP download of {file_id!r} streamed past "
                f"max_bytes ({cap}); aborting.",
                backend_name="ftp",
            )
        return bytes(buf)

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Return ``folder_id/name`` if the file exists, else None.

        We check via SIZE (cheap, RFC 3659) and fall back to a parent-
        directory listing for servers that don't accept SIZE on the
        path argument.
        """
        path = self._join(folder_id, name)
        ftp = self._connect()
        try:
            ftp.size(path)
            return path
        except (ftplib.error_perm, ftplib.error_reply):
            try:
                entries = list(_iter_entries(ftp, folder_id))
            except ftplib.error_perm:
                return None
            for entry_name, kind, _size in entries:
                if entry_name == name and kind == "file":
                    return path
            return None
        except (ftplib.error_temp, OSError):
            return None

    def copy_file(
        self,
        source_file_id: str,
        dest_folder_id: str,
        name: str,
    ) -> str:
        """FTP has no server-side copy primitive. Round-trip the file
        through memory (or a temp file for >50 MB content) and STOR
        it at the destination."""
        ftp = self._connect()
        try:
            size = ftp.size(source_file_id) or 0
        except (ftplib.error_perm, ftplib.error_reply, OSError):
            size = 0

        if size and size > 50 * 1024 * 1024:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            try:
                with open(tmp_path, "wb") as f:
                    ftp.retrbinary(
                        f"RETR {source_file_id}", f.write,
                        blocksize=_DOWNLOAD_CHUNK,
                    )
                dst = self._join(dest_folder_id, name)
                with open(tmp_path, "rb") as f:
                    ftp.storbinary(
                        f"STOR {dst}", f, blocksize=_UPLOAD_CHUNK,
                    )
                return dst
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        content = self.download_file(source_file_id)
        return self.upload_bytes(content, name, dest_folder_id)

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Return the file's SHA-256 digest, preferring the server's
        ``HASH`` / ``XSHA256`` / ``XSHA1`` / ``XMD5`` extensions when
        available, falling back to a streaming client-side hash on any
        failure (mirroring SFTP's degrade-rather-than-block posture)."""
        ftp = self._connect()
        for cmd in ("XSHA256", "HASH", "XSHA1", "XMD5"):
            try:
                resp = ftp.sendcmd(f"{cmd} {file_id}")
            except (ftplib.error_perm, ftplib.error_reply,
                    ftplib.error_temp, OSError):
                continue
            digest = _parse_hash_response(resp)
            if digest is not None:
                return digest

        try:
            hasher = hashlib.sha256()

            def _on_block(block: bytes) -> None:
                hasher.update(block)

            ftp.retrbinary(f"RETR {file_id}", _on_block, blocksize=_DOWNLOAD_CHUNK)
            return hasher.hexdigest()
        except (ftplib.error_perm, ftplib.error_reply,
                ftplib.error_temp, OSError):
            return None

    def delete_file(self, file_id: str) -> None:
        """``DELE`` ``file_id``. If the path turns out to be a
        directory (550 with a directory-flavoured message), fall back
        to ``RMD`` so the abstraction stays symmetric with SFTP/WebDAV."""
        ftp = self._connect()
        try:
            ftp.delete(file_id)
        except ftplib.error_perm as e:
            msg = str(e).lower()
            if "directory" in msg or "is a directory" in msg:
                ftp.rmd(file_id)
                return
            raise


# ──────────────────────────────────────────────────────────────────────
# Internal helpers (module-level so tests can monkeypatch them)
# ──────────────────────────────────────────────────────────────────────


def _iter_entries(
    ftp: ftplib.FTP,
    path: str,
) -> Any:
    """Yield ``(name, kind, size)`` tuples for entries directly under
    ``path``. Tries ``MLSD`` first (RFC 3659 — structured fact list)
    and falls back to ``LIST`` parsing on any 5xx that signals MLSD
    is unsupported.
    """
    try:
        for name, facts in ftp.mlsd(path):
            kind_raw = (facts.get("type") or "").lower()
            if kind_raw in ("cdir", "pdir"):
                continue
            kind = "dir" if kind_raw == "dir" else "file"
            try:
                size = int(facts.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            yield name, kind, size
        return
    except (ftplib.error_perm, ftplib.error_reply,
            ftplib.error_temp) as e:
        msg = str(e)
        if not (msg.startswith("500") or msg.startswith("501")
                or msg.startswith("502") or msg.startswith("504")
                or msg.startswith("550")):
            return
    except (AttributeError, OSError):
        pass

    raw_lines: list[str] = []
    try:
        ftp.retrlines(f"LIST {path}", raw_lines.append)
    except (ftplib.error_perm, ftplib.error_reply, OSError):
        return
    for line in raw_lines:
        parsed = _parse_list_line(line)
        if parsed is None:
            continue
        yield parsed


def _parse_list_line(line: str) -> Optional[tuple[str, str, int]]:
    """Best-effort parse of a single ``LIST`` line.

    Recognises Unix-style ``-rwxr-xr-x   1 user  grp  1234 May  9 10:15 name``
    and IIS/DOS-style ``05-09-26  10:15AM   <DIR>   name`` shapes —
    the two formats covering the vast majority of legacy hosting
    servers in the wild.
    """
    line = line.rstrip("\r\n")
    if not line:
        return None

    if line[:1] in "-dl":
        parts = line.split(None, 8)
        if len(parts) < 9:
            return None
        kind = "dir" if line[0] == "d" else "file"
        try:
            size = int(parts[4])
        except ValueError:
            size = 0
        name = parts[8]
        if " -> " in name and kind == "file":
            name = name.split(" -> ", 1)[0]
        return name, kind, size

    parts = line.split(None, 3)
    if len(parts) >= 4:
        marker = parts[2]
        rest = parts[3]
        if marker.upper() == "<DIR>":
            return rest, "dir", 0
        try:
            size = int(marker)
        except ValueError:
            return None
        return rest, "file", size

    return None


def _parse_hash_response(resp: str) -> Optional[str]:
    """Pull a hex digest out of a server's HASH/XSHA256/XSHA1/XMD5
    response line. The format isn't standardised across servers, so we
    accept any whitespace-separated token that parses as 32+ hex chars.
    """
    if not resp:
        return None
    text = resp.strip()
    if text[:3].isdigit():
        text = text[3:].lstrip(" -")
    for token in text.split():
        cleaned = token.strip(":")
        if len(cleaned) >= 32 and all(
            c in "0123456789abcdefABCDEF" for c in cleaned
        ):
            return cleaned.lower()
    return None
