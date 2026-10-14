"""Bounded ZIP/TAR extraction in a separate, cancelable process."""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import tarfile
import zipfile
from pathlib import Path

from app.domain.paths import contained, safe_relative
from app.runtime.process_limits import limit_worker


def tool_path() -> str:
    configured = os.environ.get("WORK115_EXTRACTOR_PATH", "")
    for candidate in (configured, shutil.which("7zz"), shutil.which("7z"),
                      r"C:\Program Files\7-Zip\7z.exe"):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return ""


def xtractr_path() -> str:
    configured = os.environ.get("WORK115_XTRACTR_PATH", "")
    bundled = Path(__file__).resolve().parents[2] / "bin" / (
        "115work-extract.exe" if os.name == "nt" else "115work-extract"
    )
    for candidate in (configured, str(bundled)):
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def extractor_available(config=None):
    engine = config.extract_engine if config else os.environ.get("WORK115_EXTRACT_ENGINE", "xtractr")
    if engine == "7zip":
        return bool((config and config.extractor_path and Path(config.extractor_path).is_file()) or tool_path())
    return bool((config and config.xtractr_path and Path(config.xtractr_path).is_file()) or xtractr_path())


def cue_inputs(source, helper=""):
    helper = helper or xtractr_path()
    if not helper:
        return ()
    response = subprocess.run(
        [helper], input=json.dumps({"mode": "inputs", "source": str(source.resolve())}),
        text=True, encoding="utf-8", capture_output=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        result = json.loads(response.stdout)
    except ValueError:
        raise ValueError("XTRACTR_PROCESS_FAILED") from None
    if not result.get("ok"):
        if result.get("error") == "CUE_COMPANION_NOT_READY":
            return ()
        raise ValueError(result.get("error", "XTRACTR_PROCESS_FAILED"))
    return tuple(Path(path) for path in result["inputs"])


def extract_xtractr(source, output, max_bytes, max_files):
    env = os.environ.copy()
    env.setdefault("GOMEMLIMIT", "512MiB")
    env.setdefault("GOMAXPROCS", "1")
    response = subprocess.run(
        [xtractr_path()], input=json.dumps({
            "source": str(source.resolve()), "output": str(output.resolve()),
            "password": os.environ.get("WORK115_ARCHIVE_PASSWORD", ""),
            "max_bytes": max_bytes, "max_files": max_files,
            "disable_recursion": os.environ.get("WORK115_DISABLE_RECURSION", "true") == "true",
            "recurse_iso": os.environ.get("WORK115_RECURSE_ISO", "false") == "true",
            "squash_root": os.environ.get("WORK115_SQUASH_ROOT", "false") == "true",
            "exclude_iso": os.environ.get("WORK115_EXTRACT_ISOS", "true") != "true",
        }), text=True, encoding="utf-8", capture_output=True, timeout=1800, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        result = json.loads(response.stdout)
    except (ValueError, UnicodeError):
        raise ValueError("XTRACTR_PROCESS_FAILED") from None
    if response.returncode or not result.get("ok"):
        raise ValueError(result.get("error", "XTRACTR_PROCESS_FAILED"))
    return result["manifest"] | {"source_size": source.stat().st_size,
                                 "source_mtime_ns": source.stat().st_mtime_ns}


def seven_entries(source, password, max_bytes, max_files):
    tool = tool_path()
    if not tool:
        raise ValueError("EXTRACTOR_UNAVAILABLE")
    with tempfile.TemporaryFile() as metadata:
        listed = subprocess.Popen(
            [tool, "l", "-slt", "-ba", "-sccUTF-8", "-p" + (password or "__no_password__"),
             "--", str(source)], stdout=metadata, stderr=subprocess.DEVNULL,
        )
        started = time.monotonic()
        try:
            while listed.poll() is None:
                if os.fstat(metadata.fileno()).st_size > 32 * 1024**2 or time.monotonic() - started > 120:
                    raise ValueError("ARCHIVE_METADATA_LIMIT")
                time.sleep(0.05)
            if listed.returncode:
                raise ValueError("PASSWORD_OR_MISSING_PARTS_OR_CORRUPT_ARCHIVE")
            metadata.seek(0)
            raw = metadata.read(32 * 1024**2 + 1)
            if len(raw) > 32 * 1024**2:
                raise ValueError("ARCHIVE_METADATA_LIMIT")
        finally:
            if listed.poll() is None:
                listed.kill()
                listed.wait()
    entries = []
    total_size = 0
    for block in raw.decode("utf-8", errors="strict").replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(" = ", 1) for line in block.splitlines() if " = " in line)
        if "Path" not in fields:
            continue
        if any(fields.get(key) for key in ("Symbolic Link", "Hard Link", "Reparse Point")):
            raise ValueError("ARCHIVE_SPECIAL_FILE_REJECTED")
        if any(token.startswith(("l", "b", "c", "p", "s")) for token in
               fields.get("Attributes", "").split() if len(token) > 5) or (
            fields.get("Mode", "").startswith(("l", "b", "c", "p", "s"))
        ):
            raise ValueError("ARCHIVE_SPECIAL_FILE_REJECTED")
        safe_relative(fields["Path"])
        size = int(fields.get("Size") or 0)
        total_size += size
        entries.append((fields["Path"], size, fields.get("Folder") == "+" or
                        fields.get("Attributes", "").startswith("D")))
        if len(entries) > max_files:
            raise ValueError("ARCHIVE_FILE_LIMIT")
        if total_size > max_bytes:
            raise ValueError("ARCHIVE_SIZE_LIMIT")
    return tool, entries


def extract(source: Path, output: Path, max_bytes: int, max_files: int) -> dict:
    max_bytes = max_bytes or 2**63 - 1
    output.mkdir(parents=True, exist_ok=False)
    reserve = int(os.environ.get("WORK115_LOCAL_RESERVE_BYTES", str(64 * 1024**2)))
    if shutil.disk_usage(output).free < reserve:
        raise ValueError("INSUFFICIENT_OUTPUT_SPACE")
    manifest = []
    total = 0
    seen = set()

    def publish_entry(name, size, directory, opener):
        nonlocal total
        relative = safe_relative(name)
        if len(relative.parts) > 25:
            raise ValueError("ARCHIVE_DEPTH_LIMIT")
        target = contained(output, output.joinpath(*relative.parts))
        canonical = str(target).casefold()
        if canonical in seen:
            raise ValueError("DUPLICATE_ARCHIVE_ENTRY")
        seen.add(canonical)
        if directory:
            target.mkdir(parents=True, exist_ok=True)
            return
        total += size
        if total > max_bytes:
            raise ValueError("ARCHIVE_SIZE_LIMIT")
        if len(manifest) >= max_files:
            raise ValueError("ARCHIVE_FILE_LIMIT")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest, copied = hashlib.sha256(), 0
        with opener() as reader, target.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                if shutil.disk_usage(output).free < reserve + len(chunk):
                    raise ValueError("INSUFFICIENT_OUTPUT_SPACE")
                copied += len(chunk)
                if copied > size or copied > max_bytes:
                    raise ValueError("ARCHIVE_ENTRY_SIZE_MISMATCH")
                writer.write(chunk)
                digest.update(chunk)
        if copied != size:
            raise ValueError("ARCHIVE_ENTRY_SIZE_MISMATCH")
        manifest.append({"path": relative.as_posix(), "size": copied, "sha256": digest.hexdigest()})

    password = os.environ.get("WORK115_ARCHIVE_PASSWORD", "").encode() or None
    if xtractr_path() and os.environ.get("WORK115_EXTRACT_ENGINE", "xtractr") == "xtractr":
        # Go's archive/zip does not decrypt ZIP; preserve the existing native
        # ZipCrypto path explicitly rather than silently switching engines on errors.
        encrypted_zip = False
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                encrypted_zip = any(entry.flag_bits & 1 for entry in archive.infolist())
        if not encrypted_zip:
            return extract_xtractr(source, output, max_bytes, max_files)
    native_zip = zipfile.is_zipfile(source)
    if native_zip:
        with zipfile.ZipFile(source) as probe:
            native_zip = all(entry.compress_type in {
                zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA,
            } for entry in probe.infolist())
    if native_zip:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > max_files:
                raise ValueError("ARCHIVE_FILE_LIMIT")
            if sum(x.file_size for x in entries) > max_bytes:
                raise ValueError("ARCHIVE_SIZE_LIMIT")
            for entry in entries:
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (
                    stat.S_ISREG(mode) or stat.S_ISDIR(mode)
                )):
                    raise ValueError("ARCHIVE_SPECIAL_FILE_REJECTED")
                publish_entry(entry.filename, entry.file_size, entry.is_dir(),
                              lambda e=entry: archive.open(e, pwd=password))
    elif tarfile.is_tarfile(source):
        with tarfile.open(source) as archive:
            for index, entry in enumerate(archive):
                if index >= max_files or (not entry.isfile() and not entry.isdir()):
                    raise ValueError("ARCHIVE_SPECIAL_FILE_OR_COUNT_REJECTED")
                publish_entry(entry.name, entry.size, entry.isdir(),
                              lambda e=entry: archive.extractfile(e))
    else:
        if os.environ.get("WORK115_EXTRACT_ENGINE", "xtractr") != "7zip":
            raise ValueError("XTRACTR_UNAVAILABLE")
        tool, entries = seven_entries(source, password.decode() if password else "", max_bytes, max_files)
        for name, size, directory in entries:
            if directory:
                publish_entry(name, size, True, None)
                continue
            process = subprocess.Popen(
                [tool, "x", "-so", "-spd", "-y", "-p" + (
                    password.decode() if password else "__no_password__"
                ), "--", str(source), name],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            try:
                publish_entry(name, size, False, lambda: process.stdout)
                if process.wait(timeout=30):
                    raise ValueError("PASSWORD_OR_MISSING_PARTS_OR_CORRUPT_ARCHIVE")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    if not manifest:
        raise ValueError("ARCHIVE_HAS_NO_FILES")
    result = {"files": manifest, "total_bytes": total, "source_size": source.stat().st_size,
              "source_mtime_ns": source.stat().st_mtime_ns}
    # Manifest outside extracted content cannot be overwritten by an archive entry.
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--max-files", type=int, required=True)
    args = parser.parse_args()
    try:
        # Go reserves a large virtual address arena on Linux. GOMEMLIMIT bounds
        # its heap target; containers additionally enforce physical memory limits.
        limit_worker(1024**3 if os.name == "nt" or not xtractr_path() else 8 * 1024**3, 1800)
        result = extract(Path(args.source), Path(args.output), args.max_bytes, args.max_files)
        print(json.dumps({"ok": True, "manifest": result}))
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) else (
            "PASSWORD_OR_CORRUPT_ARCHIVE" if isinstance(exc, (RuntimeError, zipfile.BadZipFile))
            else "EXTRACTION_FAILED"
        )
        print(json.dumps({"ok": False, "error": code[:100]}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
