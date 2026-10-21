"""Install the immutable helper once; emit a non-secret descriptor for packaging."""

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "packaging" / "KeychainHelper.swift"
DEST = ROOT / "data" / "security" / "EmailReaderKeychain"
MANIFEST = DEST.with_suffix(".json")
DESCRIPTOR = ROOT / "build" / "keychain-helper.json"
REQUIREMENT = '=identifier "com.privateemailreader.keychain" and certificate root = H"630f3bda14d0a6d97d87095eb7aab18ff26d4396"'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare():
    source_hash = digest(SOURCE)
    if DEST.exists():
        manifest = json.loads(MANIFEST.read_text())
        if manifest["source_sha256"] != source_hash or manifest["sha256"] != digest(DEST):
            raise RuntimeError(
                "Keychain helper changed. Do not silently replace the trusted executable."
            )
    else:
        DEST.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="helper-build-", dir=ROOT / "build") as temp:
            candidate = Path(temp) / "EmailReaderKeychain"
            subprocess.run(["xcrun", "swiftc", "-O", str(SOURCE), "-o", str(candidate)], check=True)
            subprocess.run(
                [
                    "codesign",
                    "--force",
                    "--sign",
                    "Email Reader Local Signing",
                    "--identifier",
                    "com.privateemailreader.keychain",
                    str(candidate),
                ],
                check=True,
            )
            subprocess.run(
                ["codesign", "--verify", "--strict", "-R", REQUIREMENT, str(candidate)], check=True
            )
            candidate.chmod(0o700)
            candidate.replace(DEST)
        manifest = {"source_sha256": source_hash, "sha256": digest(DEST), "path": str(DEST)}
        MANIFEST.write_text(json.dumps(manifest))
        MANIFEST.chmod(0o600)
    subprocess.run(["codesign", "--verify", "--strict", "-R", REQUIREMENT, str(DEST)], check=True)
    DESCRIPTOR.write_text(json.dumps({"path": str(DEST), "sha256": digest(DEST)}))
    print("Verified immutable Keychain helper (no credentials read)")


if __name__ == "__main__":
    prepare()
