"""StorageBackend implementation for SFTP servers.

Layout mirrors WebDAVBackend (path-as-id, username+key/password auth, no
native push). Uses `paramiko` as the SSH/SFTP transport. The classifier
maps paramiko + low-level network exceptions to `ErrorClass`; uploads
are atomic (.tmp + posix_rename) so a partial transfer never leaves a
truncated file at the destination path.

Authentication preference order: a private-key file when configured,
falling back to password (and to the user's ssh-agent / default key
search via `look_for_keys=True` and `allow_agent=True`). Host-key
verification defaults to strict — an unknown host fingerprint aborts
the connection rather than silently trusting a possible MITM. The opt-
out (`sftp_strict_host_check=False`) is intended only for one-shot LAN
test setups; production configs must keep it on.
"""
from __future__ import annotations

import errno
import io
import json
import os
import shlex
import socket
import stat as stat_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

try:
    import paramiko
except ImportError:
    raise ImportError(
        "paramiko is required for the SFTP backend.\n"
        "Install it with:  pip install paramiko"
    )

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure


# Streaming download chunk size — large enough to amortise per-call
# overhead, small enough that the cap check fires well before runaway
# downloads can OOM the client.
_DOWNLOAD_CHUNK = 64 * 1024


class SFTPBackend(StorageBackend):
    """StorageBackend implementation for SFTP servers (any RFC 4254 host)."""

    backend_name = "sftp"

    def __init__(self, config: Config) -> None:
        self.config = config
        # Single shared SSH connection (one TCP socket).
        self._client: Optional[paramiko.SSHClient] = None
        # Per-thread SFTPClient channels — paramiko's SFTPClient is
        # NOMINALLY thread-safe but its operations multiplex through one
        # request/response channel. Under concurrent load (e.g. snapshot
        # blob fan-out with parallel_workers=5) the channel queue stalls,
        # leaving most workers blocked while only one progresses. Giving
        # each worker thread its own SFTP channel (over the same SSH
        # connection — paramiko Transport supports many channels per
        # connection) restores real parallelism without paying the TCP
        # handshake cost per worker. threading.local cleans up channel
        # references on thread exit; close() also drops them explicitly.
        import threading as _threading
        self._tls = _threading.local()

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    def _connect(self) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        """Establish (or reuse) the shared SSH connection AND return a
        thread-local SFTPClient.

        Caches the SSHClient on `self._client` so repeated calls in the
        same process share one TCP connection. The SFTPClient (channel)
        is per-thread — each worker thread that touches `_connect` gets
        its own channel multiplexed over the shared connection. See the
        `__init__` comment for why per-thread channels matter for
        parallel performance.

        Host-key policy is derived from `sftp_strict_host_check` —
        strict mode (default) rejects unknown fingerprints; relaxed mode
        auto-adds them and is intended only for closed LAN test setups.
        """
        # Fast path: shared SSH ready, this thread already has a channel.
        existing_sftp = getattr(self._tls, "sftp", None)
        if self._client is not None and existing_sftp is not None:
            return self._client, existing_sftp
        # Open the shared SSH connection if it hasn't been established yet.
        if self._client is not None and existing_sftp is None:
            sftp = self._client.open_sftp()
            self._tls.sftp = sftp
            return self._client, sftp

        ssh = paramiko.SSHClient()

        # Load known_hosts so previously-seen fingerprints are honoured.
        kh_path = os.path.expanduser(
            self.config.sftp_known_hosts_file or "~/.ssh/known_hosts"
        )
        if os.path.exists(kh_path):
            try:
                ssh.load_host_keys(kh_path)
            except (IOError, OSError):
                # Unreadable known_hosts is a config issue; fall through
                # and let the policy below decide what to do with the
                # unrecognised host.
                pass

        # Strict mode: refuse to connect to a host whose key isn't in
        # known_hosts. This is the only defence against an attacker
        # standing up a MITM that re-presents a valid TLS-ish handshake
        # but a different host key. Relaxed mode (AutoAdd) is ONLY safe
        # on a closed LAN where you'd be inspecting fingerprints by hand.
        if self.config.sftp_strict_host_check:
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Resolve auth material. A user-supplied key wins over a
        # password; if both are set we still pass the password as a
        # fallback so paramiko can retry on key failure.
        key_file = self.config.sftp_key_file
        if key_file:
            key_file = os.path.expanduser(key_file)
            if not os.path.exists(key_file):
                # Treat a missing key like "no key specified" — paramiko
                # falls back to the agent / default keys / password.
                key_file = ""

        password = self.config.sftp_password or None

        ssh.connect(
            hostname=self.config.sftp_host,
            port=self.config.sftp_port,
            username=self.config.sftp_username,
            key_filename=key_file or None,
            password=password,
            look_for_keys=True,
            allow_agent=True,
            timeout=10,
        )
        sftp = ssh.open_sftp()

        self._client = ssh
        # First channel goes into thread-local for the calling thread;
        # each subsequent thread that calls _connect opens its own.
        self._tls.sftp = sftp
        return ssh, sftp

    def _mkdir_p(self, sftp: paramiko.SFTPClient, path: str) -> None:
        """Recursively create `path` on the server, ignoring already-exists."""
        if not path or path == "/":
            return
        parts = [p for p in path.split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else f"/{part}"
            try:
                sftp.mkdir(current)
            except IOError as e:
                # errno 21 (EEXIST) — directory already there, fine.
                # Some servers don't set errno on EEXIST; fall back to
                # a string check.
                code = getattr(e, "errno", None)
                msg = str(e).lower()
                if code == errno.EEXIST or "exists" in msg:
                    continue
                # If the entry exists as a non-directory, surface the
                # error — caller needs to know.
                try:
                    attr = sftp.stat(current)
                    if stat_mod.S_ISDIR(attr.st_mode):
                        continue
                except IOError:
                    pass
                raise

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw paramiko / network / IO exception to an ErrorClass.

        Matrix:
          AuthenticationException, BadHostKeyException → AUTH
          NoValidConnectionsError, socket.timeout,
              ConnectionResetError, ConnectionRefusedError → TRANSIENT
          IOError errno 2  (NO_SUCH_FILE)               → NOT_FOUND* (FILE_REJECTED)
          IOError errno 3  (NO_SUCH_PATH on some servers) → PERMISSION
          IOError "quota"/"disk full"/"no space"        → QUOTA
          everything else                                → UNKNOWN

        *The base ErrorClass enum has no NOT_FOUND member; FILE_REJECTED
        carries the same "skip this one file" semantics, which is what
        the orchestrator needs.
        """
        try:
            if isinstance(exc, paramiko.ssh_exception.AuthenticationException):
                return ErrorClass.AUTH
            if isinstance(exc, paramiko.ssh_exception.BadHostKeyException):
                return ErrorClass.AUTH
            if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
                return ErrorClass.TRANSIENT
            if isinstance(exc, (socket.timeout, ConnectionResetError, ConnectionRefusedError)):
                return ErrorClass.TRANSIENT
            if isinstance(exc, IOError):
                code = getattr(exc, "errno", None)
                if code == 2:
                    return ErrorClass.FILE_REJECTED
                if code == 3:
                    return ErrorClass.PERMISSION
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
        """Open a connection, ensure the project folder exists, persist a
        non-secret 'verified at' marker to token_file.

        The token file deliberately stores NO credentials — SSH keys stay
        on disk under user control, and passwords (when configured) live
        in the YAML config alongside the host. Writing creds twice would
        only widen the blast radius.
        """
        ssh, sftp = self._connect()

        folder = self.config.sftp_folder
        try:
            sftp.stat(folder)
        except IOError:
            # Folder doesn't exist yet — create the full path.
            self._mkdir_p(sftp, folder)

        token_path = Path(self.config.token_file)
        write_token_secure(token_path, json.dumps({
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "host": self.config.sftp_host,
        }))

    def get_credentials(self) -> paramiko.SFTPClient:
        """Return a connected SFTPClient, raising if the user hasn't
        finished `claude-mirror auth` yet."""
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            raise RuntimeError(
                "SFTP not authenticated — run claude-mirror auth"
            )
        _, sftp = self._connect()
        return sftp

    @property
    def sftp(self) -> paramiko.SFTPClient:
        # _connect returns this thread's SFTPClient (caches in _tls).
        _, sftp = self._connect()
        return sftp

    @property
    def ssh(self) -> paramiko.SSHClient:
        if not self._client:
            self.get_credentials()
        return self._client

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _join(parent: str, name: str) -> str:
        return f"{parent.rstrip('/')}/{name}"

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Create `name` under `parent_id` if absent; return its full path.

        SFTP `mkdir` raises IOError if the directory exists; we treat
        EEXIST (errno 21) and any "exists"-flavoured error as success
        rather than a hard failure, mirroring `mkdir -p` semantics.
        """
        path = self._join(parent_id, name)
        sftp = self.sftp
        try:
            sftp.mkdir(path)
        except IOError as e:
            code = getattr(e, "errno", None)
            msg = str(e).lower()
            if code == errno.EEXIST or "exists" in msg:
                return path
            # Some servers report "Failure" with no errno; check via stat.
            try:
                attr = sftp.stat(path)
                if stat_mod.S_ISDIR(attr.st_mode):
                    return path
            except IOError:
                pass
            raise
        return path

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Walk `rel_path` components, mkdir-ing intermediates. Returns
        (parent_path, basename)."""
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
        exclude_folder_names: Optional[set] = None,
    ) -> list[dict]:
        """Recursive walk via `listdir_attr`. md5 is None — SFTP has no
        native checksum, so the engine falls back to size+mtime+local-hash."""
        excluded = exclude_folder_names or set()
        results: list[dict] = []
        folders_done = 0
        files_seen = 0

        def _walk(path: str, rel_prefix: str) -> None:
            nonlocal folders_done, files_seen
            try:
                entries = self.sftp.listdir_attr(path)
            except IOError:
                return
            folders_done += 1
            for attr in entries:
                name = attr.filename
                if not name or name in (".", ".."):
                    continue
                child_path = self._join(path, name)
                child_rel = f"{rel_prefix}{name}" if rel_prefix else name
                if stat_mod.S_ISDIR(attr.st_mode):
                    if name in excluded:
                        continue
                    _walk(child_path, f"{child_rel}/")
                else:
                    results.append({
                        "id": child_path,
                        "name": name,
                        "relative_path": child_rel,
                        "size": attr.st_size,
                        "md5Checksum": None,
                    })
                    files_seen += 1
                    if progress_cb and (files_seen % 50 == 0):
                        progress_cb(folders_done, files_seen)

        _walk(folder_id, prefix)
        if progress_cb:
            progress_cb(folders_done, files_seen)
        return results

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        """List subfolders. If `name` given, filter to exact match."""
        results: list[dict] = []
        try:
            entries = self.sftp.listdir_attr(parent_id)
        except IOError:
            return results
        for attr in entries:
            if not stat_mod.S_ISDIR(attr.st_mode):
                continue
            fname = attr.filename
            if not fname or fname in (".", ".."):
                continue
            if name is not None and fname != name:
                continue
            mtime = getattr(attr, "st_mtime", None)
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

    # Block size for the manual put-loop. Matches paramiko's internal
    # default (32 KiB) — small enough that a slow connection still
    # throttles smoothly, large enough to amortise SSH packet overhead.
    _UPLOAD_CHUNK_BYTES: int = 32 * 1024

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Upload via atomic .tmp + posix_rename.

        Atomicity matters: a crashed `put` would otherwise leave a
        truncated file at the destination path that the next listing
        would advertise as the canonical content. Writing to .tmp first
        and renaming (POSIX rename is atomic on the same filesystem)
        guarantees the destination is either the old content or the new
        content, never a half-written mix. On error we attempt to remove
        the orphan .tmp so the next push doesn't trip over it.

        Resume behaviour: paramiko's SFTPClient does not support
        protocol-level resume of a partially-uploaded transfer. A
        crashed upload re-uploads from scratch on the next retry; the
        .tmp + posix_rename dance ensures the destination path is
        either the old or new content, never a mid-transfer torso.

        v0.5.39+: optional bandwidth cap via `max_upload_kbps`. We
        switch from `sftp.put` to a manual block loop so the throttle
        bucket can pre-pay each block before it's written.

        progress_callback: optional `Callable[[int], None]`. Invoked
        with delta bytes per block written. Note: paramiko's native
        `SFTPClient.put()` callback reports CUMULATIVE bytes; we keep
        the manual block loop here (already used for throttling) so
        the contract stays delta-based across all backends without an
        ad-hoc bridge.
        """
        if file_id:
            dst = file_id
        else:
            parent, basename = self.resolve_path(rel_path, root_folder_id)
            dst = self._join(parent, basename)

        sftp = self.sftp
        tmp = f"{dst}.tmp"
        bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))
        chunk_size = self._UPLOAD_CHUNK_BYTES
        try:
            with open(local_path, "rb") as src, sftp.open(tmp, "wb") as dst_f:
                # 32 KiB write buffer matches paramiko's default; setting
                # it explicitly keeps the manual loop's per-call cost
                # comparable to `sftp.put` — no regression on uncapped
                # throughput.
                try:
                    dst_f.set_pipelined(True)
                except AttributeError:
                    pass
                while True:
                    block = src.read(chunk_size)
                    if not block:
                        break
                    bucket.consume(len(block))
                    dst_f.write(block)
                    if progress_callback is not None:
                        progress_callback(len(block))
            sftp.posix_rename(tmp, dst)
        except Exception:
            # Best-effort cleanup of the partial .tmp.
            try:
                sftp.remove(tmp)
            except Exception:
                pass
            raise
        return dst

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bytes:
        """Stream-download in 64 KiB chunks, aborting past MAX_DOWNLOAD_BYTES.

        Even though SFTP doesn't have the chunked-encoding-lying-about-
        size attack surface that HTTP backends do, a hostile server can
        still feed an arbitrarily long stream. The streaming cap is the
        single line of defence against runaway memory growth on the
        client side.

        progress_callback: optional `Callable[[int], None]`. Invoked with
        delta bytes per read chunk for live progress.
        """
        sftp = self.sftp
        buf = bytearray()
        try:
            with sftp.open(file_id, "rb") as f:
                while True:
                    chunk = f.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if len(buf) > self.MAX_DOWNLOAD_BYTES:
                        raise BackendError(
                            ErrorClass.FILE_REJECTED,
                            f"SFTP download of {file_id!r} streamed past "
                            f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting.",
                            backend_name="sftp",
                        )
                    if progress_callback is not None:
                        progress_callback(len(chunk))
        except BackendError:
            raise
        return bytes(buf)

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        """Atomic write of `content` as `name` under `folder_id`. mimetype
        is ignored — SFTP has no MIME concept."""
        if file_id:
            dst = file_id
        else:
            dst = self._join(folder_id, name)

        sftp = self.sftp
        tmp = f"{dst}.tmp"
        try:
            with sftp.open(tmp, "wb") as f:
                f.write(content)
            sftp.posix_rename(tmp, dst)
        except Exception:
            try:
                sftp.remove(tmp)
            except Exception:
                pass
            raise
        return dst

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Return the full path if `name` exists in `folder_id`, else None."""
        path = self._join(folder_id, name)
        try:
            self.sftp.stat(path)
        except IOError as e:
            if getattr(e, "errno", None) == errno.ENOENT or getattr(e, "errno", None) == 2:
                return None
            # No errno (some servers): treat any stat failure as "missing".
            return None
        return path

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Server-side `cp -p` first; fall back to download+upload.

        SFTP itself has no native copy primitive — paramiko exposes the
        protocol verbatim. Most production servers are full *nix shells
        with `cp` available, so issuing one `exec_command` saves a
        round-trip-heavy get/put for big files. We fall back to the
        client-side path on any exec failure (no shell, exit != 0,
        permission denied) so the call still succeeds — just slower.
        """
        dst = self._join(dest_folder_id, name)
        cmd = f"cp -p {shlex.quote(source_file_id)} {shlex.quote(dst)}"
        try:
            ssh = self.ssh
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=30)
            try:
                stdin.close()
            except Exception:
                pass
            exit_status = stdout.channel.recv_exit_status()
            if exit_status == 0:
                return dst
        except Exception:
            pass

        # Fallback: round-trip via download_file + upload_bytes.
        content = self.download_file(source_file_id)
        return self.upload_bytes(content, name, dest_folder_id)

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Server-side `sha256sum`; None on any failure (caller falls back).

        SFTP has no in-protocol checksum, so we shell out. Many *nix
        boxes have `sha256sum`; macOS-flavoured servers have `shasum -a
        256`. We try `sha256sum` first and return None on any error so
        the caller can degrade to size+mtime comparison rather than
        block on an avoidable round-trip per file.
        """
        cmd = f"sha256sum {shlex.quote(file_id)}"
        try:
            ssh = self.ssh
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=30)
            try:
                stdin.close()
            except Exception:
                pass
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                return None
            data = stdout.read()
            if isinstance(data, bytes):
                text = data.decode("utf-8", errors="replace")
            else:
                text = str(data)
            text = text.strip()
            if len(text) < 64:
                return None
            digest = text[:64]
            # 64 hex chars only.
            if all(c in "0123456789abcdefABCDEF" for c in digest):
                return digest.lower()
            return None
        except Exception:
            return None

    def delete_file(self, file_id: str) -> None:
        """Remove a file. If it turns out to be a directory (errno 21),
        remove it as a directory instead — keeps the abstraction symmetric
        with how WebDAV's DELETE handles collections.
        """
        sftp = self.sftp
        try:
            sftp.remove(file_id)
        except IOError as e:
            code = getattr(e, "errno", None)
            msg = str(e).lower()
            if code == errno.EISDIR or code == 21 or "is a directory" in msg or "directory" in msg:
                sftp.rmdir(file_id)
                return
            raise
