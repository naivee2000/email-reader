"""Install an already verified candidate after a cold encrypted rollback snapshot.

Run only after Email Reader has exited normally. This script never opens the
database, invokes Keychain, contacts a provider, or handles decrypted mail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require_closed() -> None:
    result = subprocess.run(["pgrep", "-x", "Email Reader"], capture_output=True, check=False)
    if result.returncode != 1:
        raise RuntimeError("Close Email Reader normally before installing; nothing was replaced")


def signing_requirement(app: Path) -> str:
    result = subprocess.run(["codesign", "-d", "-r-", str(app)], capture_output=True, text=True, check=True)
    requirements = [line for line in (result.stdout + result.stderr).splitlines()
                    if line.startswith("designated => ")]
    if len(requirements) != 1 or 'identifier "com.privateemailreader.app"' not in requirements[0]:
        raise RuntimeError("The private app signing identity could not be verified")
    return requirements[0]


def install(candidate: Path, project: Path) -> dict:
    candidate = candidate.resolve(strict=True)
    if (candidate.name != "Email Reader.app"
            or not candidate.is_relative_to(project / "build" / "staged")):
        raise ValueError("Choose an existing staged Email Reader.app in this workspace")
    require_closed()
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(candidate)], check=True)
    installed = project / "dist" / "Email Reader.app"
    if signing_requirement(candidate) != signing_requirement(installed):
        raise RuntimeError("Candidate signing identity differs; nothing was replaced")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    data = project / "data"
    sources = [data / name for name in ("email_reader.db", "email_reader.db-wal", "email_reader.db-shm", "attachments")]
    sources.extend((data / "attachments").rglob("*"))
    if data.is_symlink() or (data / "recovery").is_symlink() or any(path.is_symlink() for path in sources):
        raise RuntimeError("Unexpected symlink in encrypted snapshot sources; nothing was replaced")
    snapshot = data / "recovery" / ("pre-responsiveness-" + stamp)
    snapshot.mkdir(mode=0o700)
    manifest = {"created_at": stamp, "scope": "encrypted database, WAL and original blobs", "files": {}}
    for name in ("email_reader.db", "email_reader.db-wal", "email_reader.db-shm"):
        source = data / name
        if source.is_file():
            if name == "email_reader.db":
                with source.open("rb") as stream:
                    if stream.read(16) == b"SQLite format 3\x00":
                        raise RuntimeError("Expected an encrypted production database")
            shutil.copy2(source, snapshot / name)
    if (data / "attachments").is_dir():
        # APFS copy-on-write clone; originals remain independently recoverable.
        subprocess.run(["cp", "-cR", str(data / "attachments"), str(snapshot / "attachments")], check=True)
    for saved in sorted(snapshot.rglob("*")):
        if saved.is_file():
            relative = saved.relative_to(snapshot)
            original = data / relative
            checksum = digest(saved)
            if checksum != digest(original):
                raise RuntimeError("Rollback verification failed; app was not replaced")
            manifest["files"][str(relative)] = checksum
    if "email_reader.db" not in manifest["files"]:
        raise RuntimeError("Production database missing; app was not replaced")
    (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (snapshot / "manifest.json").chmod(0o600)
    require_closed()
    archive = project / "build" / "package-backups" / ("Email Reader-" + stamp + ".zip")
    archive.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(installed), str(archive)], check=True)
    subprocess.run(["unzip", "-tq", str(archive)], check=True, stdout=subprocess.DEVNULL)
    require_closed()
    old = candidate.parent / "previous-installation.app"
    if old.exists():
        raise RuntimeError("Unexpected previous candidate; nothing was replaced")
    installed.rename(old)
    try:
        candidate.rename(installed)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(installed)], check=True)
    except Exception:
        if installed.exists():
            installed.rename(candidate)
        old.rename(installed)
        raise
    # Exact bundle just archived and verified, not a wildcard or data directory.
    shutil.rmtree(old)
    return {"installed": str(installed), "previous_app_zip": str(archive),
            "encrypted_snapshot": str(snapshot), "verified_files": len(manifest["files"])}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(install(args.candidate, Path(__file__).resolve().parent.parent), indent=2))
