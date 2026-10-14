import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.adapters.p115 import AdapterError
from app.domain.links import extract_links, match_name
from app.persistence.models import (
    Batch, BatchItem, DateFolder, Offline, Operation, ResourceLink,
    OfflineExecution, Settings,
)
from app.scheduler.store import enqueue
from app.services.events import record


POLLABLE = {"submitted", "downloading", "unknown", "submission_unknown"}
RETRYABLE = {"failed", "submit_failed"}
WINDOW_SECONDS = 90
SCAN_DELAYS = (120, 300)
POLL_DEADLINE_SECONDS = 86400


class OfflineService:
    def __init__(self, db, adapter, job_store):
        self.db, self.adapter, self.job_store = db, adapter, job_store
        self.import_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.date_lock = asyncio.Lock()
        self.snapshot = None

    def register(self, session, text: str, auto_extract: bool = True, now: float | None = None):
        links, errors = extract_links(text)
        if not links and not errors:
            raise ValueError("NO_LINKS")
        now = time.time() if now is None else now
        date = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        root = self.db.settings()["offline_root_cid"]
        batch = Batch(target_date=date, root_cid=root, auto_extract=auto_extract,
                      invalid_count=len(errors), created_at=now)
        session.add(batch)
        session.flush()
        created = duplicates = 0
        for link in links:
            existing = session.scalar(select(Offline).where(
                Offline.account_id == "115-main", Offline.identity == link.identity
            ))
            duplicate = existing is not None
            if existing is None:
                existing = Offline(
                    identity=link.identity, original_link=link.original, name=link.name,
                    kind=link.kind, size=link.size, hash_value=link.hash_value,
                    batch_id=batch.id, target_date=date, root_cid=root,
                    auto_extract=auto_extract, created_at=now, updated_at=now,
                )
                session.add(existing)
                session.flush()
                created += 1
                record(session, existing.id, "REGISTERED", "offline_submit", "Link registered")
            else:
                duplicates += 1
            session.add(BatchItem(batch_id=batch.id, task_id=existing.id, duplicate=duplicate))
        if created:
            enqueue(session, "offline-submit:" + batch.id, "offline_submit", {"batch_id": batch.id})
            window = next((x for x in session.scalars(select(Settings).where(
                Settings.key.startswith("offline-window:"),
            )) if x.value["root"] == root and x.value["date"] == date
                and now < x.value["start"] + WINDOW_SECONDS), None)
            ids = list(session.scalars(select(Offline.id).where(Offline.batch_id == batch.id)))
            if window:
                window.value = window.value | {"ids": window.value["ids"] + ids}
            else:
                session.add(Settings(key="offline-window:" + batch.id, value={
                    "root": root, "date": date, "start": now, "ids": ids, "round": 0,
                }))
        record(session, batch.id, "IMPORT", "offline_submit",
               f"Queued {created}; duplicates {duplicates}; invalid {len(errors)}")
        return {"batch_id": batch.id, "queued": created, "duplicates": duplicates,
                "invalid": len(errors), "message": f"Queued {created}; duplicates {duplicates}; invalid {len(errors)}"}

    async def import_text(self, text: str, auto_extract: bool = True, now: float | None = None):
        async with self.import_lock:
            with self.db.session() as session:
                return self.register(session, text, auto_extract, now)

    def schedule_discovery(self, session, now):
        for window in session.scalars(select(Settings).where(Settings.key.startswith("offline-window:"))):
            value = window.value
            number = value["round"]
            if number >= 2 or now < value["start"] + SCAN_DELAYS[number]:
                continue
            grouped = {}
            for identifier in value["ids"]:
                item = session.get(Offline, identifier)
                if not item or not item.target_cid or item.status in {"failed", "submit_failed", "canceled"}:
                    continue
                if session.scalar(select(ResourceLink.id).where(ResourceLink.offline_id == identifier)):
                    continue
                grouped.setdefault(item.target_cid, []).append(identifier)
            for cid, ids in grouped.items():
                enqueue(session, "scan-cid:" + cid, "cloud_scan", {
                    "cid": cid, "offline_only": True, "offline_ids": ids, "one_shot": True,
                    "reason": "离线窗口首扫" if number == 0 else "离线窗口唯一补扫",
                })
                record(session, cid, "DISCOVERY_SCHEDULED", "discover",
                       f"目标 CID {cid}，本轮 {len(ids)} 条，第 {number + 1} 次扫描")
            window.value = value | {"round": number + 1}

    async def target_folder(self, root: str, date: str) -> str:
        async with self.date_lock:
            with self.db.session() as session:
                cached = session.scalar(select(DateFolder).where(
                    DateFolder.root_cid == root, DateFolder.target_date == date,
                    DateFolder.account_id == "115-main",
                ))
                if cached:
                    return cached.cid
            from app.services.folders import durable_folder
            cid = await durable_folder(self.db, self.adapter, root, date,
                                       f"date-folder:115-main:{root}:{date}")
            with self.db.session() as session:
                session.add(DateFolder(root_cid=root, target_date=date, cid=cid))
            return cid

    async def submit_job(self, job) -> float | None:
        with self.db.session() as session:
            self.job_store.owned(session, job)
            tasks = list(session.scalars(select(Offline).where(
                Offline.batch_id == job.payload["batch_id"], Offline.status == "queued"
            ).order_by(Offline.created_at).limit(10)))
        if not tasks:
            return None
        cid = await self.target_folder(tasks[0].root_cid, tasks[0].target_date)
        with self.db.session() as session:
            self.job_store.owned(session, job)
            batch = session.get(Batch, job.payload["batch_id"])
            batch.target_cid = cid
            for item in tasks:
                current = session.get(Offline, item.id)
                key = f"offline:{current.id}:{current.generation}"
                operation = session.get(Operation, key)
                if operation and operation.status in {"UNKNOWN", "PREPARED", "ACCEPTED"}:
                    raise RuntimeError("SUBMISSION_MUST_RECONCILE")
                session.merge(Operation(key=key, task_id=current.id, kind="offline_submit"))
                current.target_cid, current.status = cid, "submitting"
                session.add(OfflineExecution(offline_id=current.id, generation=current.generation,
                            target_date=current.target_date, target_cid=cid,
                            auto_extract=current.auto_extract, status="submitting"))
        try:
            await self.adapter.submit([t.original_link for t in tasks], cid)
        except AdapterError as exc:
            with self.db.session() as session:
                self.job_store.owned(session, job)
                for item in tasks:
                    current = session.get(Offline, item.id)
                    operation = session.get(Operation, f"offline:{current.id}:{current.generation}")
                    operation.status = "UNKNOWN" if exc.uncertain else "REJECTED"
                    current.status = "submission_unknown" if exc.uncertain else "submit_failed"
                    current.error = exc.code
                    execution = session.scalar(select(OfflineExecution).where(
                        OfflineExecution.offline_id == current.id, OfflineExecution.generation == current.generation))
                    execution.status, execution.error = current.status, current.error
                    current.next_check = time.time() + 15 if exc.uncertain else None
                    record(session, current.id, current.status, "offline_submit", exc.code, "offline")
            return time.time() + 2 if len(tasks) == 10 else None
        with self.db.session() as session:
            self.job_store.owned(session, job)
            for item in tasks:
                current = session.get(Offline, item.id)
                session.get(Operation, f"offline:{current.id}:{current.generation}").status = "ACCEPTED"
                current.status, current.next_check = "submitted", time.time() + 15
                current.error = ""
                execution = session.scalar(select(OfflineExecution).where(
                    OfflineExecution.offline_id == current.id, OfflineExecution.generation == current.generation))
                execution.status = current.status
                record(session, current.id, "SUBMITTED", "offline_download", "Submitted", "offline")
        self.snapshot = None
        return time.time() + 2 if len(tasks) == 10 else None

    def schedule_poll(self, session, now: float, manual: bool = False):
        if not manual:
            self.pause_expired(session, now)
        query = select(Offline).where(Offline.status.in_(POLLABLE))
        if not manual:
            query = query.where(Offline.next_check <= now)
        if session.scalar(query.limit(1)):
            enqueue(session, "offline-poll:115-main", "offline_poll", {"manual": manual})

    def pause_expired(self, session, now):
        tasks = session.scalars(select(Offline).where(
            Offline.status.in_(POLLABLE), Offline.next_check.is_not(None)))
        for task in tasks:
            execution = session.scalar(select(OfflineExecution).where(
                OfflineExecution.offline_id == task.id,
                OfflineExecution.generation == task.generation))
            started = execution.created_at if execution else task.created_at
            if now >= started + POLL_DEADLINE_SECONDS:
                task.next_check, task.stage = None, "offline_manual"
                record(session, task.id, "OFFLINE_POLL_PAUSED", "offline_manual",
                       "离线状态自动查询已满24小时，等待手动查询；未判定下载失败")

    async def poll(self, job=None, now: float | None = None, manual: bool = False):
        now = time.time() if now is None else now
        async with self.poll_lock:
            with self.db.session() as session:
                if not manual:
                    self.pause_expired(session, now)
                query = select(Offline).where(Offline.status.in_(POLLABLE))
                if not manual:
                    query = query.where(Offline.next_check <= now)
                tasks = list(session.scalars(query))
            if not tasks:
                return
            if self.snapshot and now - self.snapshot[0] < 20:
                statuses = self.snapshot[1]
            else:
                statuses = await self.adapter.offline_snapshot()
                self.snapshot = (now, statuses)
            with self.db.session() as session:
                if job:
                    self.job_store.owned(session, job)
                for item in tasks:
                    current = session.get(Offline, item.id)
                    if current is None or current.status not in POLLABLE:
                        continue
                    matches = [
                        remote for remote in statuses
                        if (current.hash_value and current.hash_value == remote.hash_value)
                        or current.original_link == remote.link
                    ]
                    if not matches:
                        matches = [remote for remote in statuses
                                   if remote.name and match_name(remote.name) == match_name(current.name)]
                    status = matches[0].status if len(matches) == 1 else "unknown"
                    if status != current.status:
                        current.status, current.updated_at = status, now
                        current.error = "REMOTE_OFFLINE_FAILED" if status == "failed" else ""
                        record(session, current.id, status.upper(), "offline_download",
                               current.error or status, "offline")
                    current.check_count += 1
                    execution = session.scalar(select(OfflineExecution).where(
                        OfflineExecution.offline_id == current.id, OfflineExecution.generation == current.generation))
                    if execution:
                        execution.status, execution.error = current.status, current.error
                    interval = {1: 180, 2: 300}.get(
                        current.check_count, self.db.settings()["offline_fallback_seconds"]
                    )
                    started = execution.created_at if execution else current.created_at
                    expired = now >= started + POLL_DEADLINE_SECONDS
                    current.next_check = (
                        min(now + interval, started + POLL_DEADLINE_SECONDS)
                        if status in POLLABLE and not expired else None
                    )
                    if status in POLLABLE:
                        current.stage = "offline_manual" if expired else "offline_download"
                    if status == "success":
                        current.stage = "discover"

    def bind_candidates(self, session, cid: str, candidates: list, task_for_file, offline_ids=None):
        tasks = list(session.scalars(select(Offline).where(
            Offline.target_cid == cid,
            Offline.status.in_(list(POLLABLE | {"success"})),
        )))
        if offline_ids is not None:
            tasks = [item for item in tasks if item.id in offline_ids]
        for item in tasks:
            if session.scalar(select(ResourceLink).where(ResourceLink.offline_id == item.id)):
                continue
            if item.kind == "magnet":
                directories = [file for file in candidates if file.is_dir and
                               match_name(file.name) == match_name(item.name)]
                if len(directories) == 1:
                    parents = {directories[0].fid}
                    while True:
                        expanded = parents | {file.fid for file in candidates if file.is_dir and file.cid in parents}
                        if expanded == parents:
                            break
                        parents = expanded
                    children = [file for file in candidates if not file.is_dir and file.cid in parents]
                    if children:
                        archive_count = 0
                        for file in children:
                            task = task_for_file(session, file, item.auto_extract, offline_id=item.id)
                            if task is None:
                                continue
                            session.add(ResourceLink(offline_id=item.id, task_id=task.id,
                                                     evidence="magnet-directory"))
                            from app.services.archives import is_archive
                            if is_archive(file.name):
                                archive_count += 1
                            if item.auto_extract and is_archive(file.name):
                                record(session, task.id, "CLOUD_TAKEOVER", task.stage,
                                       "离线产物已关联，云解压队列接管", "cloud")
                        item.status = "success"
                        item.stage = "join_review" if archive_count else "complete"
                        item.error = "MAGNET_SCOPE_COMPLETENESS_REQUIRES_REVIEW" if archive_count else ""
                        item.next_check = None
                        continue
            scored = []
            for file in candidates:
                if file.is_dir:
                    continue
                same_name = match_name(file.name) == match_name(item.name)
                same_size = bool(item.size) and item.size == file.size
                score = 30 if same_name and same_size else 20 if same_name else 10 if same_size else 0
                if score:
                    scored.append((score, file))
            if not scored:
                continue
            best_score = max(x[0] for x in scored)
            best = [f for score, f in scored if score == best_score]
            if len(best) != 1:
                item.stage, item.error = "discover", "AMBIGUOUS_RESOURCE_MATCH"
                continue
            file = best[0]
            task = task_for_file(session, file, item.auto_extract, offline_id=item.id)
            if task is None:
                if item.status != "success":
                    record(session, item.id, "SUCCESS", "complete",
                           "离线产物已发现，非压缩包不进入解压队列", "offline")
                item.status, item.stage, item.error, item.next_check = "success", "complete", "", None
                continue
            session.add(ResourceLink(offline_id=item.id, task_id=task.id, evidence=str(best_score)))
            from app.services.archives import is_archive
            if item.auto_extract and is_archive(file.name):
                record(session, task.id, "CLOUD_TAKEOVER", task.stage,
                       "离线完成，原包已关联并交给云解压队列", "cloud")
            if item.status != "success":
                record(session, item.id, "SUCCESS", "discover", "File discovered", "offline")
            item.status, item.stage, item.error, item.next_check = "success", task.stage, "", None

    def retry(self, task_id: str | None = None) -> int:
        with self.db.session() as session:
            query = select(Offline).where(Offline.status.in_(RETRYABLE))
            if task_id:
                query = query.where(Offline.id == task_id)
            tasks = list(session.scalars(query))
            if task_id and not tasks:
                raise ValueError("ONLY_TERMINAL_FAILURE_CAN_RETRY")
            for item in tasks:
                item.status, item.error, item.check_count = "queued", "", 0
                item.retry_count += 1
                item.generation += 1
                enqueue(session, "offline-submit:" + item.batch_id, "offline_submit",
                        {"batch_id": item.batch_id})
                key = f"offline-window:retry:{item.id}:{item.generation}"
                session.add(Settings(key=key, value={
                    "root": item.root_cid, "date": item.target_date,
                    "start": time.time(), "ids": [item.id], "round": 0,
                }))
                record(session, item.id, "MANUAL_RETRY", "offline_submit", "Retry authorized")
        return len(tasks)

    def recover(self, session):
        for item in session.scalars(select(Offline).where(Offline.status == "submitting")):
            item.status, item.next_check = "submission_unknown", time.time()
            operation = session.get(Operation, f"offline:{item.id}:{item.generation}")
            if operation:
                operation.status = "UNKNOWN"
        for item in session.scalars(select(Offline).where(Offline.status == "queued")):
            enqueue(session, "offline-submit:" + item.batch_id, "offline_submit",
                    {"batch_id": item.batch_id})
