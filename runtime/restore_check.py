"""Verify a complete encrypted backup by restoring it into a disposable database."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from common import ROOT


def command(*args: str, input_file=None) -> None:
    subprocess.run(
        args,
        stdin=input_file,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        timeout=300,
    )


def run(backup: Path | None = None) -> None:
    files = sorted((ROOT / "backups").glob("platform-*.enc"), key=lambda p: p.stat().st_mtime)
    backup = backup or (files[-1] if files else None)
    if not backup or not backup.is_file():
        raise FileNotFoundError("No encrypted platform backup")

    key = (ROOT / "backup.key").read_bytes()
    if len(key) != 32:
        raise ValueError("Invalid backup key length")

    # A unique name prevents interference with the platform's ordinary test DB.
    database = "inovens_restore_" + str(os.getpid())
    created = False
    with tempfile.TemporaryDirectory(prefix="inovens-restore-") as directory:
        directory = Path(directory)
        archive = directory / "backup.tar"
        with backup.open("rb") as source, archive.open("wb") as target:
            if source.read(8) != b"INOVENS2":
                raise ValueError("Unknown backup format")
            nonce = source.read(12)
            source.seek(-16, os.SEEK_END)
            tag = source.read(16)
            source.seek(20)
            remaining = backup.stat().st_size - 20 - 16
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("Truncated backup")
                remaining -= len(block)
                target.write(decryptor.update(block))
            target.write(decryptor.finalize())  # Verifies the authentication tag.

        dump = directory / "platform.dump"
        with tarfile.open(archive) as contents:
            member = contents.getmember("platform.dump")
            with contents.extractfile(member) as source, dump.open("wb") as target:
                shutil.copyfileobj(source, target)

        try:
            command("docker", "exec", "inovens-platform-db", "createdb", "-U", "inovens", database)
            created = True
            with dump.open("rb") as source:
                command(
                    "docker", "exec", "-i", "inovens-platform-db", "pg_restore",
                    "-U", "inovens", "-d", database, "--no-owner", "--no-privileges",
                    input_file=source,
                )
            query = "SELECT CASE WHEN count(*)>0 THEN 'ok' ELSE 'empty' END FROM platform_users"
            result = subprocess.run(
                ["docker", "exec", "inovens-platform-db", "psql", "-U", "inovens", "-d", database, "-At", "-c", query],
                capture_output=True, text=True, check=True, timeout=30,
            )
            if result.stdout.strip() != "ok":
                raise RuntimeError("Restored database failed application data check")
        finally:
            if created:
                command("docker", "exec", "inovens-platform-db", "dropdb", "-U", "inovens", "--if-exists", "--force", database)
    print("Encrypted backup authenticated and restored to disposable database:", backup.name)


if __name__ == "__main__":
    run(Path(os.sys.argv[1]) if len(os.sys.argv) > 1 else None)
