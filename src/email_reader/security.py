from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from email_reader import process_launch


class SecretVault(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class KeychainVault:
    """Use a hash-pinned immutable helper; it authenticates this app's signature.

    Self-signed Keychain clients get cdhash partition ACLs, so even a stable
    designated requirement does not survive rebuilds. The helper is never
    rebuilt alongside the app. Secrets travel only over private process pipes.
    """

    @staticmethod
    def _ensure_signed_app() -> None:
        if not getattr(sys, "frozen", False):
            raise RuntimeError(
                "Private email data can only be opened by the signed Email Reader app. "
                "Use --demo when running from source."
            )

    def _request(self, operation: str, name: str, value: str | None = None) -> dict:
        self._ensure_signed_app()
        descriptor = Path(__file__).parent / "keychain-helper.json"
        try:
            config = json.loads(descriptor.read_text())
            helper = Path(config["path"])
            if hashlib.sha256(helper.read_bytes()).hexdigest() != config["sha256"]:
                raise ValueError("helper fingerprint mismatch")
        except (OSError, ValueError, KeyError) as from_error:
            raise RuntimeError(
                "The trusted Keychain helper is missing or changed. Reinstall Email Reader."
            ) from from_error
        request = {"operation": operation, "name": name}
        if value is not None:
            request["value"] = value
        try:
            process = process_launch.run(
                [str(helper)],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "Keychain approval timed out. Reopen Email Reader when ready."
            ) from None
        if process.returncode:
            raise RuntimeError(
                "Keychain access was not approved or the calling app was not trusted."
            )
        try:
            result = json.loads(process.stdout)
            if not isinstance(result, dict) or (
                operation == "get" and not isinstance(result.get("value"), (str, type(None)))
            ):
                raise ValueError("invalid response")
            return result
        except ValueError:
            raise RuntimeError("The Keychain helper returned an invalid response.") from None

    def get(self, name: str) -> str | None:
        return self._request("get", name).get("value")

    def set(self, name: str, value: str) -> None:
        self._request("set", name, value)

    def delete(self, name: str) -> None:
        self._request("delete", name)


class MemoryVault:
    """Test-only vault that never writes secrets to disk."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def get_or_create_master_key(vault: SecretVault) -> bytes:
    encoded = vault.get("master-key-v1")
    if encoded:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
        if len(key) != 32:
            raise ValueError("The Keychain master key is invalid")
        return key
    key = secrets.token_bytes(32)
    vault.set("master-key-v1", base64.urlsafe_b64encode(key).decode("ascii"))
    return key


def database_passphrase(master_key: bytes) -> str:
    digest = hashlib.sha256(b"sqlcipher-v1\0" + master_key).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


class EncryptedBlobStore:
    MAGIC = b"PER1"

    def __init__(self, root: Path, master_key: bytes) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o777 != 0o700:
            self.root.chmod(0o700)
        self._cipher = AESGCM(master_key)

    def put(self, content: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(content).hexdigest()
        target = self.root / digest[:2] / f"{digest}.enc"
        if target.exists():
            return digest, len(content)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = secrets.token_bytes(12)
        encrypted = self._cipher.encrypt(nonce, content, digest.encode("ascii"))
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(self.MAGIC + nonce + encrypted)
        temporary.chmod(0o600)
        os.replace(temporary, target)
        return digest, len(content)

    def get(self, digest: str) -> bytes:
        path = self.root / digest[:2] / f"{digest}.enc"
        payload = path.read_bytes()
        if not payload.startswith(self.MAGIC) or len(payload) < 17:
            raise ValueError("Encrypted attachment has an invalid header")
        nonce = payload[4:16]
        return self._cipher.decrypt(nonce, payload[16:], digest.encode("ascii"))

    def verify(self, digest: str) -> bool:
        return hashlib.sha256(self.get(digest)).hexdigest() == digest

    def delete(self, digest: str) -> None:
        path = self.root / digest[:2] / f"{digest}.enc"
        path.unlink(missing_ok=True)

    @contextmanager
    def decrypted_file(self, digest: str, filename: str) -> Iterator[Path]:
        safe_name = Path(filename).name or "attachment"
        with tempfile.TemporaryDirectory(prefix="email-reader-") as tmp:
            tmp_path = Path(tmp)
            tmp_path.chmod(0o700)
            output = tmp_path / safe_name
            output.write_bytes(self.get(digest))
            output.chmod(0o600)
            yield output
