"""Backward-compatibility shim — DriveClient is now in backends.googledrive."""
from .backends.googledrive import GoogleDriveBackend as DriveClient, SCOPES

__all__ = ["DriveClient", "SCOPES"]
