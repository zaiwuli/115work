import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.adapters.p115 import AdapterError, CloudFile
from app.domain.links import match_name
from app.domain.rules import evaluate
from app.domain.rules import DOCUMENT_SUFFIXES, OFFLINE_SOURCE
from pathlib import PurePosixPath
from app.persistence.models import Identity, Operation, Resource, Settings, Task
from app.scheduler.store import enqueue
from app.services.events import record, transition
from app.services.execution import operation_scope
from app.services.fallback import approve_automatic, expire_cloud


ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz",
                    ".gz", ".bz2", ".xz", ".iso", ".ar", ".br", ".brotli", ".cpgz",
                    ".cpio", ".cpio.gz", ".deb", ".gzip", ".lz4", ".lz", ".lzip", ".lzma",
                    ".lzma2", ".s2", ".rpm", ".snappy", ".sz", ".tbz", ".tbz2", ".tlz",
                    ".txz", ".tz", ".tar.z", ".z", ".zlib", ".zst", ".zstd", ".zz", ".cue")


def is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


class ArchiveService:
    def __init__(self, db, adapter, offline, store, secrets):
        self.db, self.adapter, self.offline, self.store, self.secrets = (
            db, adapter, offline, store, secrets,
        )

    def adopt(self, session, file: CloudFile, auto_extract: bool = True,
              source_id: str = "", offline_id: str = "") -> Task | None:
        from app.persistence.models import DateFolder, Offline
        settings = self.db.settings()
        offline_directory = source_id == OFFLINE_SOURCE or bool(offline_id) or (
            bool(settings.get("offline_root_cid")) and file.cid == settings["offline_root_cid"]
        ) or bool(session.scalar(select(DateFolder.id).where(
            DateFolder.root_cid == settings.get("offline_root_cid"),
            DateFolder.cid == file.cid,
        )))
        if offline_directory:
            source_id = OFFLINE_SOURCE
        if not is_archive(file.name):
            from app.persistence.models import Offline, Source
            if not source_id and offline_id:
                parent = session.get(Offline, offline_id)
                sources = list(session.scalars(select(Source).where(
                    Source.kind == "115", Source.enabled,
                    Source.config["cid"].as_string() == parent.root_cid,
                ))) if parent else []
                if len(sources) == 1:
                    source_id = sources[0].id
            document_policy = evaluate(session, source_id, file.name, file.size)
            if (PurePosixPath(file.name).suffix.lower() not in DOCUMENT_SUFFIXES or
                (document_policy.get("document_action", "register") == "register"
                 and not document_policy.get("document_links"))):
                return None
        object_key = f"115:115-main:{file.fid}:{file.size}" + (":" + file.sha1 if file.sha1 else "")
        existing = session.scalar(select(Task).where(Task.object_key == object_key).order_by(
            Task.created_at.desc()
        ))
        if existing and existing.state not in {"CANCELED", "SKIPPED"}:
            if existing.state not in {"SUCCEEDED", "FAILED"} or session.get(Identity, existing.content_key):
                record(session, existing.id, "DUPLICATE_SKIPPED", existing.stage,
                       f"同一云文件已有任务，复用任务 {existing.id}，文件ID {file.fid}")
                return existing
        archive = is_archive(file.name)
        content_key = f"{match_name(file.name)}:{file.size}"
        identity = session.get(Identity, content_key) if archive else None
        conflict = False
        if identity:
            prior = session.get(Task, identity.task_id)
            prior_hash = prior.locator.get("sha1", "")
            if not file.sha1 or not prior_hash or prior_hash == file.sha1:
                record(session, prior.id, "DUPLICATE_SKIPPED", prior.stage,
                       "压缩包名称与大小命中已有身份，未重复解压")
                return prior
            content_key += ":" + file.sha1
            conflict = True
        if file.sha1:
            resource = session.scalar(select(Resource).where(Resource.strong_hash == file.sha1,
                                                             Resource.size == file.size))
            if resource and resource.task_id:
                previous = session.get(Task, resource.task_id)
                if previous.state not in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"} or session.get(Identity, previous.content_key):
                    record(session, previous.id, "DUPLICATE_SKIPPED", previous.stage,
                           "文件 SHA1 与大小命中已处理资源，未重复解压")
                    return previous
        policy = evaluate(session, source_id, file.name, file.size)
        from app.persistence.models import Source
        source = session.get(Source, source_id) if source_id else None
        from app.persistence.models import Offline
        all_offline = list(session.scalars(select(Offline).where(
            Offline.target_cid == file.cid, Offline.status.not_in(["failed", "submit_failed", "canceled"]),
        )))
        if offline_id:
            parent_offline = session.get(Offline, offline_id)
            if parent_offline:
                all_offline = [parent_offline]
        if not source and all_offline:
            roots = {item.root_cid for item in all_offline}
            root_sources = list(session.scalars(select(Source).where(
                Source.kind == "115", Source.enabled,
                Source.config["cid"].as_string().in_(roots),
            )))
            if len(root_sources) == 1:
                source = root_sources[0]
                if not offline_directory:
                    source_id = source.id
                    policy = evaluate(session, source_id, file.name, file.size)
        matching_offline = [item for item in all_offline
            if match_name(item.name) == match_name(file.name) and
            (not item.size or item.size == file.size)]
        if offline_id and all_offline:
            auto_extract = all_offline[0].auto_extract
        elif matching_offline and not all(item.auto_extract for item in matching_offline):
            auto_extract = False
        settings = self.db.settings()
        policy = {"squash_root": True, **policy}
        if settings.get("cloud_archive_cid"):
            policy.setdefault("archive_cid", settings["cloud_archive_cid"])
        if settings.get("cloud_failed_cid"):
            policy.setdefault("cloud_failed_cid", settings["cloud_failed_cid"])
        task = Task(
            object_key=object_key, content_key=content_key, source="115", name=file.name,
            size=file.size, stage="cloud_prepare" if archive and auto_extract else "registered",
            state="READY" if archive and auto_extract else "SUCCEEDED",
            locator=asdict(file) | {"source_id": source_id},
            policy=policy | {"auto_extract": auto_extract, "retain_original": True,
                            **({"source_revision": source.revision} if source else {}),
                            "cloud_output_cid": (source.config.get("extract_cid")
                                if source and source.config.get("extract_cid")
                                else (settings.get("offline_extract_cid") if all_offline else "")
                                or settings["cloud_output_cid"]),
                            "output_date": policy.get("output_date", settings["cloud_extract_by_date"]),
                            "adopt_date": (all_offline[0].target_date if all_offline else
                                datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"))},
        )
        session.add(task)
        session.flush()
        resource = session.scalar(select(Resource).where(Resource.object_key == object_key))
        if resource:
            resource.task_id, resource.version = task.id, asdict(file)
        else:
            session.add(Resource(object_key=object_key, name=file.name, size=file.size,
                                 strong_hash=file.sha1, version=asdict(file), task_id=task.id))
        record(session, task.id, "DISCOVERED", task.stage, file.name, "cloud")
        if archive and auto_extract:
            if conflict:
                transition(session, task, "WAITING_APPROVAL", "identity_conflict",
                           error="KNOWN_CONTENT_HASH_CONFLICT")
            elif policy.get("workflow") == "ignore":
                transition(session, task, "SKIPPED", "registered")
                session.merge(Identity(key=content_key, task_id=task.id, result="ignored"))
            elif policy.get("workflow") == "local":
                task.stage = "local_fallback"
                task.state = "WAITING_APPROVAL"
            else:
                enqueue(session, "archive:" + task.id, "cloud_extract", {"task_id": task.id})
        else:
            task.completed_at = time.time()
            if not archive and PurePosixPath(file.name).suffix.lower() in DOCUMENT_SUFFIXES:
                if policy.get("document_action", "register") != "register" or policy.get("document_links"):
                    task.state, task.stage, task.completed_at = "READY", "document_copy", None
                    enqueue(session, "document-copy:" + task.id, "document_copy", {"task_id": task.id})
        return task

    async def scan(self, job):
        cid = job.payload["cid"]
        settings = self.db.settings()
        if cid in {settings.get(key) for key in (
            "cloud_output_cid", "cloud_archive_cid", "cloud_failed_cid",
        ) if settings.get(key) and settings.get(key) != "0"}:
            return None
        started = time.monotonic()
        offset = int(job.payload.get("offset", 0))
        collected = []
        next_offset = None
        walking = job.payload.get("walk", [])
        for _ in range(0 if walking else 5):
            page = await self.adapter.files(cid, offset)
            collected.extend(page.files)
            next_offset = page.next_offset
            if next_offset is None:
                break
            offset = next_offset
        if walking:
            collected = []
            for _ in range(5):
                if not walking:
                    break
                directory = walking.pop(0)
                page = await self.adapter.files(directory["cid"], directory.get("offset", 0))
                collected.extend(page.files)
                walking.extend({"cid": f.fid} for f in page.files if f.is_dir)
                if page.next_offset is not None:
                    walking.insert(0, {"cid": directory["cid"], "offset": page.next_offset})
            next_offset = None
        elif next_offset is None:
            from app.persistence.models import Offline
            with self.db.session() as session:
                magnet_names = {match_name(x.name) for x in session.scalars(select(Offline).where(
                    Offline.target_cid == cid, Offline.kind == "magnet",
                ))}
            directories = collected + [CloudFile(**x) for x in job.payload.get("candidates", [])]
            walking = [{"cid": f.fid} for f in directories if f.is_dir and match_name(f.name) in magnet_names]
        with self.db.session() as session:
            current_job = self.store.owned(session, job)
            candidate_rows = current_job.payload.get("candidates", []) + [asdict(x) for x in collected]
            candidates = list({(x["fid"], x["cid"]): x for x in candidate_rows}.values())
            # Keep only bounded matching evidence; scans never assert source deletion.
            if len(candidates) > 10000:
                raise AdapterError("DISCOVERY_BUDGET_REQUIRES_REVIEW")
            if not job.payload.get("offline_only"):
                for file in collected:
                    if not file.is_dir and (is_archive(file.name) or
                                           PurePosixPath(file.name).suffix.lower() in DOCUMENT_SUFFIXES):
                        from app.persistence.models import Source
                        sources = list(session.scalars(select(Source).where(
                            Source.kind == "115", Source.enabled,
                            Source.config["cid"].as_string() == cid,
                        )))
                        if len(sources) > 1:
                            raise ValueError("AMBIGUOUS_CLOUD_SOURCE")
                        self.adopt(session, file, source_id=job.payload.get("source_id") or
                                   (sources[0].id if sources else ""))
            if next_offset is not None or walking:
                current_job.payload = current_job.payload | {"offset": next_offset or 0,
                                                     "candidates": candidates, "walk": walking}
                return time.time() + 5
            self.offline.bind_candidates(
                session, cid, [CloudFile(**x) for x in candidates], self.adopt,
                current_job.payload.get("offline_ids"),
            )
            record(session, cid, "SCAN_COMPLETED", "discover",
                   f"触发：{job.payload.get('reason', '手动检查')}；目标 CID {cid}；"
                   f"本次读取 {len(collected)} 项，累计候选 {len(candidates)} 项；"
                   f"耗时 {time.monotonic() - started:.2f} 秒")
            current_job.payload = current_job.payload | {"offset": 0, "candidates": [], "walk": [],
                                   "discovery_started": job.payload.get("discovery_started", time.time())}
            waiting = session.scalar(select(Task).where(
                Task.source == "115", Task.stage == "cloud_prepare", Task.state == "READY",
            ))
            if waiting:
                enqueue(session, "archive:" + waiting.id, "cloud_extract", {"task_id": waiting.id})
        # Visibility retry is separate from offline status polls.
        with self.db.session() as session:
            from app.persistence.models import Offline, ResourceLink
            unresolved = session.scalar(select(Offline).where(
                Offline.target_cid == cid, Offline.status == "success",
                ~select(ResourceLink.id).where(ResourceLink.offline_id == Offline.id).exists(),
            ))
        if unresolved and time.time() - job.payload.get("discovery_started", time.time()) > self.config.discovery_timeout:
            with self.db.session() as session:
                from app.persistence.models import Offline
                for item in session.scalars(select(Offline).where(
                    Offline.target_cid == cid, Offline.status == "success", Offline.stage == "discover",
                )):
                    item.stage, item.error = "discover_review", "DISCOVERY_DEADLINE_REQUIRES_REVIEW"
            return None
        return None

    def fail_content(self, session, task, code: str) -> float | None:
        task.error = code
        if task.attempts < 3:
            transition(session, task, "WAITING_RETRY", "cloud_prepare", error=code)
            task.remote_id = ""
            task.output = {}
            return time.time() + self.db.settings()["cloud_retry_seconds"]
        transition(session, task, "WAITING_APPROVAL", "local_fallback", error=code)
        task.wait_reason = "APPROVAL"
        session.merge(Identity(key=task.content_key, task_id=task.id, result="cloud_failed"))
        if task.policy.get("cloud_failed_cid"):
            task.output = task.output | {"failure_archive_result": "PENDING"}
            enqueue(session, "failure-archive:" + task.id, "failure_archive", {"task_id": task.id})
            return None
        approve_automatic(session, task)
        return None

    async def run(self, job) -> float | None:
        task_id = job.payload["task_id"]
        settings = self.db.settings()
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Task, task_id)
            # A queued cloud job must not reclaim a task switched to local execution.
            if task.policy.get("fallback_approved") or task.stage in {"cache", "local_extract"}:
                return None
            if task.state in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED", "WAITING_APPROVAL"}:
                active = session.get(Settings, "cloud_active")
                if active and active.value.get("value") == task_id:
                    active.value = {"value": ""}
                return None
            if task.cancel_requested and not task.remote_id and task.stage in {
                "cloud_prepare", "cloud_inspect", "cloud_password",
            }:
                self.stop_preparation(session, task, "CANCELED")
                return None
            active = session.get(Settings, "cloud_active")
            active_id = active.value["value"] if active else ""
            if active_id and active_id != task_id:
                previous = session.get(Task, active_id)
                if not previous or previous.state in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED", "WAITING_APPROVAL"}:
                    active_id = ""
                    active.value = {"value": ""}
            cooldown = session.get(Settings, "cloud_cooldown")
            if active_id and active_id != task_id:
                return time.time() + 10
            if not active_id and cooldown and cooldown.value["value"] > time.time():
                return cooldown.value["value"]
            session.merge(Settings(key="cloud_active", value={"value": task_id}))
            started = task.policy.get("cloud_started_at")
            if not started:
                started = time.time()
                task.policy = task.policy | {"cloud_started_at": started}
            if time.time() - started >= settings["cloud_extract_timeout_seconds"]:
                expire_cloud(session, task)
                return None
            if task.cancel_requested and not task.remote_id and task.stage == "cloud_prepare":
                transition(session, task, "CANCELED", task.stage)
                session.merge(Settings(key="cloud_active", value={"value": ""}))
                return None
        file = task.locator
        if not file.get("pickcode") or not task.name.lower().endswith((".zip", ".rar", ".7z")):
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(Task, task_id)
                transition(session, current, "WAITING_APPROVAL", "local_fallback",
                           error="CLOUD_CAPABILITY_NOT_AVAILABLE")
                session.merge(Settings(key="cloud_active", value={"value": ""}))
            return None
        try:
            if task.stage == "cloud_prepare":
                passwords = [""] + [self.secrets.decrypt(x) for x in settings["passwords"]]
                password = passwords[min(task.password_attempts, len(passwords) - 1)]
                key = f"cloud-prepare:{operation_scope(task)}:{task.attempts + 1}:{task.password_attempts}"
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task_id)
                    operation = session.get(Operation, key)
                    if operation is None:
                        session.add(Operation(key=key, task_id=task_id, kind="cloud_prepare"))
                        current.attempts += 1 if task.password_attempts == 0 else 0
                    # Persist the observation stage before calling a mutating endpoint.
                    transition(session, current, "WAITING_EXTERNAL", "cloud_inspect")
                state = (
                    await self.adapter.inspect_extract(file["pickcode"]) if operation
                    else await self.adapter.prepare_extract(file["pickcode"], password)
                )
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task_id)
                    session.get(Operation, key).status = "ACCEPTED"
                    transition(session, current, "WAITING_EXTERNAL", "cloud_inspect")
                return await self._preparation_result(job, state)
            if task.stage in {"cloud_inspect", "cloud_password"}:
                state = await self.adapter.inspect_extract(file["pickcode"])
                return await self._preparation_result(job, state)
            if task.stage == "cloud_output":
                output_cid = task.output.get("cid")
                if not output_cid:
                    from app.services.folders import durable_folder, output_folder
                    root_cid = task.policy["cloud_output_cid"]
                    if task.policy.get("output_date"):
                        root_cid = await durable_folder(
                            self.db, self.adapter, root_cid, task.policy["adopt_date"],
                            f"output-date:{root_cid}:{task.policy['adopt_date']}", task_id,
                        )
                    output_cid = await output_folder(
                        self.db, self.adapter, root_cid,
                        PurePosixPath(task.name).stem[:80],
                        f"output-folder:{task_id}:{task.attempts}", task_id,
                    )
                    with self.db.session() as session:
                        self.store.owned(session, job)
                        session.get(Task, task_id).output = {"cid": output_cid}
                key = f"cloud-export:{operation_scope(task)}:{task.attempts}"
                with self.db.session() as session:
                    self.store.owned(session, job)
                    operation = session.get(Operation, key)
                    if operation:
                        if operation.status == "REJECTED":
                            return self.fail_content(session, session.get(Task, task_id),
                                                     "CLOUD_EXPORT_REJECTED")
                        current = session.get(Task, task_id)
                        transition(session, current, "RECONCILING", "cloud_export_unknown",
                                   error="REMOTE_OPERATION_ID_REQUIRED")
                        return None
                    session.add(Operation(key=key, task_id=task_id, kind="cloud_export"))
                remote_id = await self.adapter.extract(file["pickcode"], output_cid)
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task_id)
                    current.remote_id = remote_id
                    transition(session, current, "WAITING_EXTERNAL", "cloud_progress")
                    record(session, task_id, "RUNNING", "cloud_progress",
                           "云端开始解压", "cloud")
                    operation = session.get(Operation, key)
                    operation.status, operation.evidence = "ACCEPTED", {"remote_id": remote_id}
                return time.time() + 15
            if task.stage == "cloud_progress":
                state = await self.adapter.extract_status(task.remote_id)
                if state in {"running", "unknown"}:
                    return time.time() + 30
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task_id)
                    if state == "failed":
                        session.merge(Settings(key="cloud_active", value={"value": ""}))
                        return self.fail_content(session, current, "CLOUD_CONTENT_FAILED")
                    transition(session, current, "WAITING_EXTERNAL", "cloud_verify")
                return time.time()
            if task.stage == "cloud_verify":
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task_id)
                    current.output = task.output | {"verification": "remote_reported_complete"}
                    transition(session, current, "CANCELED" if current.cancel_requested else "SUCCEEDED", "complete")
                    session.merge(Identity(key=current.content_key, task_id=task_id, result="success"))
                    session.merge(Settings(key="cloud_active", value={"value": ""}))
                    session.merge(Settings(key="cloud_cooldown", value={
                        "value": time.time() + settings["cloud_cooldown_seconds"],
                    }))
                    if not current.cancel_requested and (current.policy.get("original_action", "retain") != "retain"
                                                         or current.policy.get("cleanup_enabled")
                                                         or current.policy.get("squash_root", True)):
                        enqueue(session, "postprocess:" + task_id, "postprocess", {"task_id": task_id})
                return None
        except AdapterError as exc:
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(Task, task_id)
                if current.stage in {"cloud_prepare", "cloud_inspect", "cloud_password"} and not current.remote_id:
                    if current.cancel_requested:
                        self.stop_preparation(session, current, "CANCELED")
                        return None
                    if exc.code == "REMOTE_REJECTED" and not exc.uncertain:
                        self.stop_preparation(session, current, "FAILED", exc.code)
                        approve_automatic(session, current)
                        return None
                if exc.uncertain and task.stage == "cloud_output":
                    export = session.get(Operation, f"cloud-export:{operation_scope(task)}:{task.attempts}")
                    transition(session, current, "RECONCILING",
                               "cloud_export_unknown" if export else "cloud_output", error=exc.code)
                    return None
                if not exc.uncertain and task.stage == "cloud_output" and exc.code == "REMOTE_REJECTED":
                    operation = session.get(Operation, f"cloud-export:{operation_scope(task)}:{task.attempts}")
                    if operation:
                        operation.status = "REJECTED"
                    session.merge(Settings(key="cloud_active", value={"value": ""}))
                    return self.fail_content(session, current, exc.code)
                transition(session, current, "WAITING_EXTERNAL", current.stage, error=exc.code)
                current.wait_reason = "AUTH" if "AUTH" in exc.code else "CONNECTION"
            return min(time.time() + 300,
                       task.policy.get("cloud_started_at", time.time()) + settings["cloud_extract_timeout_seconds"])
        return None

    def stop_preparation(self, session, task, state, error=""):
        transition(session, task, state, task.stage, error=error)
        task.wait_reason = ""
        active = session.get(Settings, "cloud_active")
        if active and active.value.get("value") == task.id:
            active.value = {"value": ""}
        # Preparation cannot export files; close only its journal, never export operations.
        for operation in session.scalars(select(Operation).where(
            Operation.task_id == task.id, Operation.kind == "cloud_prepare",
            Operation.status.in_(["PREPARED", "UNKNOWN", "ACCEPTED"]),
        )):
            operation.status = "REJECTED" if state == "FAILED" else "CANCELED"

    async def _preparation_result(self, job, state: str) -> float | None:
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Task, job.payload["task_id"])
            if task.cancel_requested and not task.remote_id:
                self.stop_preparation(session, task, "CANCELED")
                return None
            if state == "success":
                transition(session, task, "READY", "cloud_output")
                return time.time()
            if state == "failed":
                session.merge(Settings(key="cloud_active", value={"value": ""}))
                return self.fail_content(session, task, "CLOUD_CONTENT_FAILED")
            if state == "password":
                passwords = self.db.settings()["passwords"]
                if task.password_attempts < len(passwords):
                    task.password_attempts += 1
                    transition(session, task, "READY", "cloud_prepare")
                    return time.time() + 5
                task.wait_reason = "PASSWORD"
                transition(session, task, "WAITING_APPROVAL", "cloud_password",
                           error="PASSWORD_REQUIRED")
                session.merge(Settings(key="cloud_active", value={"value": ""}))
                return None
            return time.time() + 30
