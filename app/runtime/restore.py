"""Offline, non-overwriting restore of a sensitive backup into a new data directory."""
import argparse
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path


def restore(bundle: Path, target: Path):
    target = target.resolve()
    if target.exists():
        raise ValueError("RESTORE_REQUIRES_NEW_DATA_DIRECTORY")
    if target == Path(target.anchor):
        raise ValueError("RESTORE_ROOT_FORBIDDEN")
    with zipfile.ZipFile(bundle) as archive:
        names = {item.filename for item in archive.infolist()}
        if not {"115work.db", "master.key"}.issubset(names):
            raise ValueError("INVALID_BACKUP_BUNDLE")
        if len(names) != len(archive.infolist()):
            raise ValueError("DUPLICATE_BACKUP_MEMBER")
        if (archive.getinfo("115work.db").file_size > 2 * 1024**3 or
                archive.getinfo("master.key").file_size > 1024):
            raise ValueError("BACKUP_SIZE_LIMIT")
        key = archive.read("master.key")
        if len(key) > 1024:
            raise ValueError("BACKUP_SIZE_LIMIT")
        config = target / "config"
        config.mkdir(parents=True)
        with archive.open("115work.db") as reader, (config / "115work.db").open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
        (config / "master.key").write_bytes(key)
    with sqlite3.connect(config / "115work.db") as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("BACKUP_INTEGRITY_FAILED")
        for name, value in (("paused", True), ("adapter_mode", "disabled")):
            connection.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)",
                               (name, json.dumps({"value": value})))
        connection.execute("DELETE FROM settings WHERE key LIKE 'session:%' OR key LIKE 'qrcode:%'")
        connection.commit()
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("new_data_dir", type=Path)
    args = parser.parse_args()
    print(restore(args.bundle, args.new_data_dir))


if __name__ == "__main__":
    main()
