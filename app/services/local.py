import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.adapters.documents import ParsedDocument
from app.adapters.extract_worker import cue_inputs, extractor_available
from app.domain.links import match_name
from app.domain.paths import contained
from app.domain.rules import DOCUMENT_SUFFIXES, evaluate
from app.domain.volumes import groups
from app.persistence.models import ArchiveGroup, CacheEntry, DocumentImport, Identity, Observation, Source, Task
from app.scheduler.store import enqueue
from app.services.archives import is_archive
from app.services.events import record, transition


VOLUME_PATTERN = re.compile(r"(?i)(\.part\d+\.rar|\.r\d{2}|\.7z\.\d{3}|\.zip\.\d{3})$")


class LocalService:
    def __init__(self, db, config, store, offline, secrets=None):
        self.db, self.config, self.store, self.offline = db, config, store, offline
        self.secrets = secrets
        self.process = None
        self.transfer = None

    @staticmethod
    def bounded_files(root, limit=10000, offset=0, excludes=()):
        def excluded(path):
            return any(path.is_relative_to(prefix) for prefix in excludes)
        if excluded(root):
            return [], 0
        found = []
        seen = 0
        for parent, directories, names in os.walk(root, followlinks=False):
            directories[:] = [name for name in directories if not name.startswith(".") and
                              not (Path(parent) / name).is_symlink() and
                              not excluded(Path(parent) / name)]
            for name in names:
                path = Path(parent) / name
                if not path.is_symlink() and not excluded(path):
                    seen += 1
                    if seen <= offset:
                        continue
                    found.append(path)
                if len(found) >= limit:
                    return found, offset + len(found)
        return found, 0

    def stable(self, session, path: Path) -> bool:
        info = path.stat()
        key = str(path)
        previous = session.get(Observation, key)
        if previous is None or previous.size != info.st_size or previous.mtime != info.st_mtime:
            session.merge(Observation(key=key, size=info.st_size, mtime=info.st_mtime,
                                      stable_since=time.time()))
            return False
        return time.time() - previous.stable_since >= self.config.stable_seconds

    async def scan(self, job):
        started = time.monotonic()
        with self.db.session() as session:
            self.store.owned(session, job)
            source = session.get(Source, job.payload["source_id"])
            if not source or not source.enabled:
                return None
        root = Path(source.config["path"])
        if not root.is_dir():
            raise ValueError("SOURCE_UNAVAILABLE")
        excludes = tuple(Path(path) for path in source.config.get("exclude_paths", []))
        if source.kind == "inbox" and self.config.local_root:
            excludes += tuple(self.config.local_path(x) for x in (
                "inbox/processed", "inbox/partial", "inbox/failed",
            ))
        files, cursor = await asyncio.to_thread(self.bounded_files, root, 10000, source.cursor, excludes)
        with self.db.session() as session:
            self.store.owned(session, job)
            session.get(Source, source.id).cursor = cursor
            archives = []
            companions = {}
            for path in files:
                if path.is_symlink() or not path.is_file():
                    continue
                path = contained(root, path)
                if path.name.startswith((".", "~")) or not self.stable(session, path):
                    continue
                if source.kind == "inbox":
                    if path.suffix.lower() in {".txt", ".md", ".csv", ".json", ".pdf", ".docx"}:
                        enqueue(session, "document:" + str(path), "document",
                                {"path": str(path), "source_id": source.id})
                    continue
                if path.suffix.lower() in DOCUMENT_SUFFIXES and not is_archive(path.name):
                    info = path.stat()
                    object_key = f"document:{source.id}:{path}:{info.st_size}:{info.st_mtime_ns}"
                    if not session.scalar(select(Task).where(Task.object_key == object_key)):
                        policy = evaluate(session, source.id, path.name, info.st_size)
                        task = Task(object_key=object_key, content_key=object_key,
                                    name=path.name, size=info.st_size, source=source.kind,
                                    stage="document_copy", locator={"path": str(path), "root": str(root),
                                    "mtime_ns": info.st_mtime_ns, "source_id": source.id},
                                    policy=policy | {"source_revision": source.revision})
                        session.add(task)
                        session.flush()
                        enqueue(session, "document-copy:" + task.id, "document_copy", {"task_id": task.id})
                    continue
                if not is_archive(path.name) and not VOLUME_PATTERN.search(path.name):
                    continue
                archives.append(path)
                if path.suffix.lower() == ".cue":
                    inputs = await asyncio.to_thread(cue_inputs, path, self.config.xtractr_path)
                    safe_inputs = tuple(contained(root, part) for part in inputs)
                    if any(part.is_relative_to(prefix) for part in safe_inputs for prefix in excludes):
                        archives.pop()
                        continue
                    if safe_inputs and all(self.stable(session, part) for part in safe_inputs):
                        companions[path] = safe_inputs
            for group in groups(archives, companions):
                path = group.main
                info = path.stat()
                policy = evaluate(session, source.id, group.name, group.size)
                if path.suffix.lower() == ".iso" and not policy.get("extract_isos", True):
                    continue
                if policy.get("workflow") == "ignore":
                    continue
                content_key = f"{match_name(group.name)}:{group.size}"
                object_key = f"local:{source.id}:{path.relative_to(root)}:{group.size}:{group.key}"
                waiting_previous = list(session.scalars(select(Task).where(
                    Task.locator["source_id"].as_string() == source.id,
                    Task.locator["path"].as_string() == str(path),
                    Task.wait_reason == "PARTS", Task.state == "WAITING_RESOURCE",
                )))
                for previous in waiting_previous:
                    if previous.locator.get("group_key") != group.key:
                        transition(session, previous, "SKIPPED", previous.stage, error="NEW_VOLUME_VERSION")
                if session.get(Identity, content_key) or session.scalar(select(Task).where(
                    (Task.object_key == object_key) | (Task.content_key == content_key),
                    Task.state.not_in(["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"]),
                )):
                    continue
                for previous in session.scalars(select(Task).where(
                    Task.locator["source_id"].as_string() == source.id,
                    Task.locator["path"].as_string() == str(path),
                    Task.wait_reason == "PARTS", Task.state == "WAITING_RESOURCE",
                )):
                    transition(session, previous, "SKIPPED", previous.stage, error="NEW_VOLUME_VERSION")
                native = path.name.lower().endswith((
                    ".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz",
                ))
                unsupported = not native and not extractor_available(self.config)
                waiting = not group.complete
                session.merge(ArchiveGroup(id=group.key, source_id=source.id, name=group.name,
                                           state="READY" if group.complete else "WAITING_PARTS",
                                           manifest={"parts": [str(p) for p in group.parts], "size": group.size}))
                task = Task(
                    object_key=object_key, content_key=content_key, name=group.name,
                    size=group.size, source=source.kind,
                    state="WAITING_RESOURCE" if waiting else "WAITING_APPROVAL" if unsupported else "READY",
                    stage="local_capability" if unsupported else (
                        "local_extract"
                    ),
                    wait_reason="PARTS" if waiting else "EXTRACTOR" if unsupported else "",
                    locator={"path": str(path), "root": str(root), "mtime_ns": info.st_mtime_ns,
                             "source_id": source.id, "group_key": group.key,
                             "parts": [{"path": str(p), "size": p.stat().st_size,
                                        "mtime_ns": p.stat().st_mtime_ns} for p in group.parts]},
                    policy=policy | {"source_revision": source.revision,
                        **({"mapping_id": source.config["mapping_id"],
                            "mapping_revision": next(x["revision"] for x in self.db.settings()["mappings"]
                                if x["id"] == source.config["mapping_id"])}
                           if source.config.get("mapping_id") else {})},
                )
                session.add(task)
                session.flush()
                record(session, task.id, "DISCOVERED", task.stage, task.name, "local")
                if not unsupported and not waiting:
                    enqueue(session, "local:" + task.id, "local_extract", {"task_id": task.id})
            record(session, source.id, "LOCAL_SCAN_COMPLETED", "local_scan",
                   f"目录 {root}；扫描 {len(files)} 个文件；稳定压缩包 {len(archives)} 个；"
                   f"耗时 {time.monotonic() - started:.2f} 秒")
        return time.time() + 5 if cursor else None

    async def document(self, job):
        path = Path(job.payload["path"])
        with self.db.session() as session:
            self.store.owned(session, job)
            source = session.get(Source, job.payload["source_id"])
            if not source or not source.enabled:
                return None
        path = contained(Path(source.config["path"]), path)
        if not path.exists():
            return None
        try:
            from app.services.retention import digest
            if path.stat().st_size > self.config.max_document_bytes:
                raise ValueError("DOCUMENT_SIZE_LIMIT")
            input_digest = await asyncio.to_thread(digest, path)
            with self.db.session() as session:
                previous = session.get(DocumentImport, input_digest)
                if previous and previous.state == "FAILED":
                    return None
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.adapters.document_worker", str(path),
                "--max-bytes", str(self.config.max_document_bytes),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), self.config.document_timeout)
                result = json.loads(stdout)
                if not result["ok"]:
                    raise ValueError(result["error"])
                parsed = ParsedDocument(result["digest"], result["text"], result["encoding"])
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            async with self.offline.import_lock:
                with self.db.session() as session:
                    self.store.owned(session, job)
                    existing = session.get(DocumentImport, parsed.digest)
                    if existing is None or not existing.batch_id:
                        statistics = self.offline.register(session, parsed.text)
                        if existing is None:
                            existing = DocumentImport(digest=parsed.digest, path=str(path))
                            session.add(existing)
                        existing.batch_id = statistics["batch_id"]
                        existing.statistics = statistics | {"encoding": parsed.encoding}
                        existing.state = "IMPORTED"
            destination = self.config.local_path(
                "inbox/partial" if statistics.get("invalid") else "inbox/processed"
            ) / (
                path.stem[:80] + "-" + parsed.digest[:12] + path.suffix
            )
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).hexdigest() != parsed.digest:
                    raise ValueError("DOCUMENT_ARCHIVE_CONFLICT")
                # Preserve a repeated input; do not silently delete it.
                destination = destination.with_name(destination.stem + "-" + job.id[:8] + path.suffix)
            if hashlib.sha256(path.read_bytes()).hexdigest() != parsed.digest:
                raise ValueError("DOCUMENT_CHANGED_DURING_IMPORT")
            await asyncio.to_thread(shutil.move, str(path), str(destination))
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(DocumentImport, parsed.digest)
                current.state, current.path = "ARCHIVED", str(destination)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            code = str(exc)[:100] if isinstance(exc, ValueError) else "DOCUMENT_PARSE_FAILED"
            from app.services.retention import digest as file_digest
            digest = await asyncio.to_thread(file_digest, path)
            failed_path = self.config.local_path("inbox/failed") / (
                path.stem[:80] + "-" + digest[:12] + path.suffix
            )
            if failed_path.exists():
                failed_path = failed_path.with_name(failed_path.stem + "-" + job.id[:8] + path.suffix)
            await asyncio.to_thread(shutil.move, str(path), str(failed_path))
            with self.db.session() as session:
                self.store.owned(session, job)
                existing = session.get(DocumentImport, digest)
                if not existing:
                    session.add(DocumentImport(digest=digest, path=str(failed_path), state="FAILED", error=code))
                else:
                    existing.state, existing.path, existing.error = "FAILED", str(failed_path), code
            raise ValueError(code) from None
        return None

    async def run(self, job):
        settings = self.db.settings()
        max_bytes = settings.get("local_max_extract_bytes")
        max_bytes = self.config.max_extract_bytes if max_bytes is None else max_bytes
        max_files = settings.get("local_max_extract_files") or self.config.max_extract_files
        reserve_bytes = settings.get("local_reserve_bytes", 5 * 1024**3)
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Task, job.payload["task_id"])
            if task.state in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
                return None
            if task.cancel_requested:
                transition(session, task, "CANCELED", task.stage)
                return None
            source = session.get(Source, task.locator["source_id"]) if task.locator.get("source_id") else None
            remote = task.source == "115"
            if task.policy.get("mapping_id"):
                mapping = next((x for x in self.db.settings()["mappings"]
                    if x["id"] == task.policy["mapping_id"] and x["enabled"]
                    and x["revision"] == task.policy["mapping_revision"]), None)
                if not mapping:
                    transition(session, task, "WAITING_RESOURCE", task.stage,
                               error="MAPPING_REVISION_CHANGED")
                    return None
            if not remote and (
                not source or not source.enabled or source.revision != task.policy["source_revision"]
            ):
                transition(session, task, "WAITING_RESOURCE", task.stage, error="SOURCE_REVISION_CHANGED")
                return None
        if remote:
            path = await self.transfer.download(task, job)
            if path is None:
                return time.time() + 10
        else:
            root = Path(task.locator["root"])
            path = contained(root, Path(task.locator["path"]))
            for part in task.locator.get("parts", [task.locator]):
                part_path = contained(root, Path(part["path"]))
                info = part_path.stat()
                if info.st_size != part.get("size", task.size) or info.st_mtime_ns != part["mtime_ns"]:
                    raise ValueError("SOURCE_VERSION_CHANGED")
        output_root = Path(source.config.get("output_path") or self.config.local_path("output")) if source else (
            self.config.local_path("output")
        )
        allowed_outputs = [self.config.local_path("output")] + self.config.allowed_output_roots
        if not any(output_root.resolve().is_relative_to(root.resolve()) for root in allowed_outputs):
            raise ValueError("OUTPUT_NOT_IN_AUTHORIZED_ROOT")
        output_root.mkdir(parents=True, exist_ok=True)
        attempt = task.output.get("local_attempt", task.attempts)
        staging = output_root / (".work-" + task.id + f"-a{attempt}")
        final = output_root / task.output.get("local_final_name", Path(task.name).stem[:80])
        if "local_final_name" not in task.output:
            if final.exists():
                final = output_root / (Path(task.name).stem[:80] + "-" + task.id[:10])
                number = 2
                while final.exists():
                    final = output_root / (Path(task.name).stem[:80] + "-" + task.id[:10] + f"-{number}")
                    number += 1
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(Task, task.id)
                current.output = current.output | {"local_final_name": final.name}
        evidence_path = output_root / (".manifest-" + task.id + f"-a{attempt}" + ".json")
        if evidence_path.exists():
            manifest = json.loads(evidence_path.read_text(encoding="utf-8"))
            staging = Path(manifest.get("staging", str(staging)))
            candidate = final if final.exists() else staging
            if not self.verify_manifest(candidate, manifest):
                raise ValueError("OUTPUT_MANIFEST_MISMATCH")
        else:
            if staging.exists() or final.exists():
                with self.db.session() as session:
                    self.store.owned(session, job)
                    transition(session, session.get(Task, task.id), "RECONCILING",
                               "local_extract", error="PARTIAL_OUTPUT_REQUIRES_REVIEW")
                return None
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(Task, task.id)
                current.output = current.output | {"local_attempt": attempt}
                current.attempts += 1
                transition(session, current, "RUNNING", "local_extract")
            passwords = [""] + [self.secrets.decrypt(x) for x in self.db.settings()["passwords"]] if self.secrets else [""]
            embedded = re.search(r"\{\{(.*?)\}\}", str(path))
            if embedded and embedded[1]:
                passwords.insert(0, embedded[1])
            passwords = list(dict.fromkeys(passwords))
            for index, password in enumerate(passwords):
                attempt_staging = staging if index == 0 else staging.with_name(staging.name + f"-p{index}")
                env = os.environ.copy()
                env["WORK115_ARCHIVE_PASSWORD"] = password
                env["WORK115_EXTRACT_ENGINE"] = self.config.extract_engine
                for key, default in (("disable_recursion", False), ("recurse_iso", False),
                                     ("squash_root", True), ("extract_isos", True)):
                    env["WORK115_" + key.upper()] = str(task.policy.get(key, default)).lower()
                if self.config.xtractr_path:
                    env["WORK115_XTRACTR_PATH"] = self.config.xtractr_path
                if self.config.extractor_path:
                    env["WORK115_EXTRACTOR_PATH"] = self.config.extractor_path
                env["WORK115_LOCAL_RESERVE_BYTES"] = str(reserve_bytes)
                if shutil.disk_usage(output_root).free < reserve_bytes:
                    raise ValueError("INSUFFICIENT_OUTPUT_SPACE")
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "app.adapters.extract_worker", str(path), str(attempt_staging),
                    "--max-bytes", str(max_bytes),
                    "--max-files", str(max_files),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
                    **({"creationflags": 512} if os.name == "nt" else {"start_new_session": True}),
                )
                try:
                    communication = asyncio.create_task(self.process.communicate())
                    deadline = time.monotonic() + self.config.extract_timeout
                    try:
                        while not communication.done():
                            await asyncio.wait({communication}, timeout=0.5)
                            if shutil.disk_usage(output_root).free < reserve_bytes:
                                raise ValueError("INSUFFICIENT_OUTPUT_SPACE")
                            if time.monotonic() > deadline:
                                raise TimeoutError("EXTRACTION_TIMEOUT")
                        stdout, _ = await communication
                    finally:
                        if not communication.done():
                            await self.stop()
                            await communication
                    result = json.loads(stdout)
                    with self.db.session() as session:
                        self.store.owned(session, job)
                        session.get(Task, task.id).password_attempts = index
                    if result["ok"]:
                        manifest = result["manifest"]
                        staging = attempt_staging
                        evidence_path.write_text(json.dumps(manifest | {"staging": str(staging)}), encoding="utf-8")
                        break
                    if "PASSWORD" not in result["error"] or index == len(passwords) - 1:
                        raise ValueError(result["error"])
                except BaseException:
                    await self.stop()
                    raise
                finally:
                    self.process = None
        # Recheck owner, cancellation and input version immediately before publication.
        with self.db.session() as session:
            self.store.owned(session, job)
            current = session.get(Task, task.id)
            if current.cancel_requested:
                transition(session, current, "CANCELED", "local_extract")
                return None
            if path.stat().st_size != manifest["source_size"] or (
                path.stat().st_mtime_ns != manifest["source_mtime_ns"]
            ):
                raise ValueError("SOURCE_CHANGED_DURING_EXTRACTION")
            if not remote:
                for part in task.locator.get("parts", []):
                    part_path = contained(root, Path(part["path"]))
                    info = part_path.stat()
                    if info.st_size != part["size"] or info.st_mtime_ns != part["mtime_ns"]:
                        raise ValueError("SOURCE_CHANGED_DURING_EXTRACTION")
            if not final.exists():
                staging.rename(final)
            current.output = {"path": str(final), "manifest": str(evidence_path),
                              "verification": "sha256_manifest"}
            transition(session, current, "SUCCEEDED", "complete")
            session.merge(Identity(key=task.content_key, task_id=task.id, result="success"))
            if current.policy.get("original_action", "retain") != "retain" or current.policy.get("cleanup_enabled"):
                enqueue(session, "postprocess:" + task.id, "postprocess", {"task_id": task.id})
        return None

    @staticmethod
    def verify_manifest(root: Path, manifest: dict) -> bool:
        for entry in manifest.get("files", []):
            file = contained(root, root / entry["path"])
            if not file.is_file() or file.stat().st_size != entry["size"]:
                return False
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                return False
        return bool(manifest.get("files")) or manifest.get("cleanup_verified_empty", False)

    async def copy_cache(self, task, source: Path, job, part=None):
        part = part or {"size": task.size, "mtime_ns": task.locator["mtime_ns"]}
        expected = part["size"]
        cache = self.config.local_path("cache") / task.id
        cache.mkdir(exist_ok=True)
        destination = cache / source.name
        partial = cache / (source.name + ".part")
        marker = cache / (source.name + ".COMPLETE.json")
        if destination.exists():
            if marker.exists():
                evidence = json.loads(marker.read_text())
                from app.services.retention import digest
                if (evidence.get("version") == part and destination.stat().st_size == expected
                    and evidence.get("sha256") == await asyncio.to_thread(digest, destination)):
                    return destination
            raise ValueError("CACHE_SIZE_MISMATCH")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > expected:
            raise ValueError("CACHE_PART_SIZE_MISMATCH")
        if self.transfer and await asyncio.to_thread(self.transfer.cache_bytes) + expected - offset > self.config.cache_max_bytes:
            raise ValueError("CACHE_CAPACITY_LIMIT")
        if shutil.disk_usage(cache).free < expected - offset + 64 * 1024**2:
            raise ValueError("INSUFFICIENT_CACHE_SPACE")
        with source.open("rb") as reader, partial.open("ab") as writer:
            reader.seek(offset)
            while chunk := await asyncio.to_thread(reader.read, 1024 * 1024):
                if self.db.settings().get("downloads_paused", False):
                    return None
                with self.db.session() as session:
                    self.store.owned(session, job)
                    if session.get(Task, task.id).cancel_requested:
                        return None
                await asyncio.to_thread(writer.write, chunk)
        if source.stat().st_size != expected or source.stat().st_mtime_ns != part["mtime_ns"]:
            raise ValueError("SOURCE_VERSION_CHANGED")
        if partial.stat().st_size != expected:
            raise ValueError("CACHE_INCOMPLETE")
        partial.rename(destination)
        from app.services.retention import digest
        marker.write_text(json.dumps({"size": expected, "object_key": task.object_key, "version": part,
                                      "sha256": await asyncio.to_thread(digest, destination)}), encoding="utf-8")
        with self.db.session() as session:
            self.store.owned(session, job)
            session.merge(CacheEntry(task_id=task.id, path=str(cache), size=task.size,
                                     verification="size_version_sha256", manifest=task.locator))
        return destination

    async def stop(self):
        if self.process and self.process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(self.process.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                import signal
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()

    async def document_copy(self, job):
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Task, job.payload["task_id"])
            if task.state in {"SUCCEEDED", "CANCELED"}:
                return None
            if task.cancel_requested:
                transition(session, task, "CANCELED", task.stage)
                return None
            source_id = task.locator.get("source_id")
            if source_id:
                source = session.get(Source, source_id)
                if not source or not source.enabled or (
                    task.policy.get("source_revision") and
                    source.revision != task.policy["source_revision"]
                ):
                    transition(session, task, "WAITING_RESOURCE", task.stage, error="SOURCE_REVISION_CHANGED")
                    return None
        action = task.policy.get("document_action", "register")
        if task.policy.get("document_links") and task.size > self.config.max_document_bytes:
            raise ValueError("DOCUMENT_SIZE_LIMIT")
        if task.source == "115":
            if action == "register" and not task.policy.get("document_links"):
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task.id)
                    current.output = {"extension": Path(task.name).suffix.lower(),
                                      "mime": mimetypes.guess_type(task.name)[0] or "application/octet-stream",
                                      "processing_result": "REGISTERED"}
                    transition(session, current, "SUCCEEDED", "complete")
                return None
            path = await self.transfer.download(task, job)
            if path is None:
                return time.time() + 10
            if task.policy.get("document_links"):
                destination = self.config.local_path("inbox/links") / (task.id + path.suffix)
                if not destination.exists():
                    await asyncio.to_thread(shutil.copy2, path, destination)
                with self.db.session() as session:
                    self.store.owned(session, job)
                    source = session.scalar(select(Source).where(Source.kind == "inbox", Source.enabled))
                    if not source:
                        raise ValueError("INBOX_SOURCE_REQUIRED")
                    enqueue(session, "document:" + str(destination), "document",
                            {"source_id": source.id, "path": str(destination)})
                    current = session.get(Task, task.id)
                    current.output = {"processing_result": "LINK_DOCUMENT_CACHED",
                                      "document_path": str(destination)}
                    transition(session, current, "SUCCEEDED", "complete")
                return None
        else:
            path = contained(Path(task.locator["root"]), Path(task.locator["path"]))
        info = path.stat()
        if info.st_size != task.size or (task.source != "115" and
                                       info.st_mtime_ns != task.locator["mtime_ns"]):
            raise ValueError("SOURCE_VERSION_CHANGED")
        if action == "register":
            result = {"extension": path.suffix.lower(), "processing_result": "REGISTERED",
                      "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}
        else:
            folder = self.config.local_path("output") / "documents" / task.id
            folder.mkdir(parents=True, exist_ok=True)
            target = contained(folder, folder / path.name)
            partial = target.with_name(target.name + ".part")
            from app.services.retention import digest
            if not target.exists():
                await asyncio.to_thread(shutil.copy2, path, partial)
                if await asyncio.to_thread(digest, path) != await asyncio.to_thread(digest, partial):
                    raise ValueError("DOCUMENT_COPY_MISMATCH")
                with self.db.session() as session:
                    self.store.owned(session, job)
                    partial.rename(target)
            result = {"path": str(folder), "verification": "sha256_manifest"}
            manifest_path = folder.parent / (".manifest-" + task.id + ".json")
            manifest_path.write_text(json.dumps({"files": [{"path": path.name, "size": task.size,
                                                          "sha256": await asyncio.to_thread(digest, target)}]}))
            result["manifest"] = str(manifest_path)
        with self.db.session() as session:
            self.store.owned(session, job)
            current = session.get(Task, task.id)
            current.output = result
            transition(session, current, "SUCCEEDED", "complete")
            if action == "archive":
                current.policy = current.policy | {"original_action": "archive"}
                enqueue(session, "postprocess:" + task.id, "postprocess", {"task_id": task.id})
        return None
