import asyncio
import logging
import time
import uuid
import socket
import traceback
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.adapters.p115 import AdapterError
from app.adapters.safe_http import SafeHTTPTransport
from app.persistence.models import Event, Job, Offline, Outbox, Schedule, Source, Task, Transfer
from app.scheduler.governor import RemoteGovernor
from app.scheduler.store import JobStore, enqueue
from app.services.archives import ArchiveService
from app.services.events import record, transition
from app.services.local import LocalService
from app.services.offline import OfflineService
from app.services.retention import RetentionService
from app.services.transfers import TransferService
from app.services.cloud_monitor import CloudMonitor
from app.services.notifications import enabled as notification_enabled, ms_payload, normalize_ms_url


logger = logging.getLogger("115work")


class Engine:
    def __init__(self, db, config, adapter, secrets):
        self.db, self.config, self.adapter = db, config, adapter
        self.secrets = secrets
        if hasattr(adapter, "governor"):
            adapter.governor = RemoteGovernor(db)
        self.store = JobStore(db, uuid.uuid4().hex)
        self.offline = OfflineService(db, adapter, self.store)
        self.archives = ArchiveService(db, adapter, self.offline, self.store, secrets)
        self.archives.config = config
        self.local = LocalService(db, config, self.store, self.offline, secrets)
        self.stopping = asyncio.Event()
        self.wake = asyncio.Event()
        self.runner = None
        self.running_jobs = {}
        self.watchers = {}
        self.next_cache_cleanup = 0
        self.retention = RetentionService(db, config, self.store, adapter)
        self.local.transfer = TransferService(db, config, self.store, adapter)
        self.monitor = CloudMonitor(db, adapter)

    def initialize(self):
        self.store.recover()
        with self.db.session() as session:
            self.offline.recover(session)
            # Retired sources are historical records, never local filesystem jobs.
            for source in session.scalars(select(Source)):
                if source.name == "Local archives" and source.kind == "local":
                    source.name = "本地压缩包"
                elif source.name == "Link inbox" and source.kind == "inbox":
                    source.name = "链接收件箱"
                if source.kind not in {"local", "inbox", "115"}:
                    source.enabled = False
            for job in session.scalars(select(Job)):
                if job.kind == "cd2_scan" and job.state not in {"SUCCEEDED", "FAILED", "CANCELED"}:
                    job.state = "CANCELED"
            # Read-only jobs can be resumed; mutating workflows inspect their journals.
            for job in session.scalars(select(Job).where(Job.state == "RECONCILING")):
                job.state, job.due = "READY", time.time()
            for schedule in session.scalars(select(Schedule)):
                schedule.enabled = False
            if not session.scalar(select(Source).limit(1)):
                session.add(Source(name="本地压缩包", kind="local",
                                   config={"path": str(self.config.local_path("watch"))}))
                session.add(Source(name="链接收件箱", kind="inbox",
                                   config={"path": str(self.config.local_path("inbox/links")),
                                           "exclude_paths": [str(self.config.local_path(x)) for x in
                                               ("inbox/processed", "inbox/partial", "inbox/failed")]
                                           if self.config.local_root else []}))
            for source in session.scalars(select(Source).where(Source.enabled)):
                source.next_run = time.time()

    def plan(self):
        if self.db.settings()["paused"]:
            return
        now = time.time()
        with self.db.session() as session:
            if now >= self.next_cache_cleanup and self.db.settings().get("cache_auto_cleanup", True):
                enqueue(session, "automatic-cache-cleanup", "cache_gc", {"automatic": True})
                self.next_cache_cleanup = now + 60
            self.offline.schedule_poll(session, now)
            self.offline.schedule_discovery(session, now)
            if self.config.adapter == "p115":
                self.monitor.schedule(session, now)
            for source in session.scalars(select(Source).where(
                Source.enabled, Source.interval > 0, Source.next_run <= now,
            )):
                if source.kind == "115":
                    continue
                else:
                    enqueue(session, "scan-source:" + source.id, "local_scan", {"source_id": source.id})
                source.next_run = now + source.interval
            if session.scalar(select(Outbox).where(Outbox.status == "pending",
                                                   Outbox.next_run <= now)):
                enqueue(session, "outbox", "notifications", {})

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(15)
            if not self.store.heartbeat(job):
                raise RuntimeError("LEASE_LOST")

    async def execute(self, job):
        heartbeat = asyncio.create_task(self.heartbeat(job))
        work = None
        try:
            handler = {
                "life_monitor": self.monitor.run,
                "offline_submit": self.offline.submit_job,
                "offline_poll": self.poll_job,
                "cloud_scan": self.archives.scan,
                "cloud_extract": self.archives.run,
                "local_scan": self.local.scan,
                "local_extract": self.local.run,
                "document": self.local.document,
                "notifications": self.notify,
                "postprocess": self.retention.run,
                "cache_gc": self.retention.cleanup,
                "document_copy": self.local.document_copy,
                "failure_archive": self.retention.failed_archive,
            }.get(job.kind)
            if not handler:
                raise ValueError("UNKNOWN_JOB_KIND")
            work = asyncio.create_task(handler(job))
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                await heartbeat
            due = await work
            self.store.finish(job, due=due)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AdapterError) else (
                str(exc) if isinstance(exc, ValueError) else "INTERNAL_EXECUTION_ERROR"
            )
            logger.warning("job=%s kind=%s error=%s", job.id, job.kind, code[:100])
            if code == "INTERNAL_EXECUTION_ERROR":
                frames = traceback.extract_tb(exc.__traceback__)
                logger.warning("job=%s exception_type=%s frames=%s", job.id,
                               type(exc).__name__, " > ".join(
                                   f"{frame.name}:{frame.lineno}" for frame in frames))
            with self.db.session() as session:
                try:
                    current = self.store.owned(session, job)
                except RuntimeError:
                    return
                current.error = code[:100]
                record(session, job.id, "JOB_FAILED", job.kind,
                       f"调度任务失败；错误代码 {code[:100]}；异常类型 {type(exc).__name__}")
                if job.kind == "local_extract":
                    task = session.get(Task, job.payload["task_id"])
                    transfer = session.get(Transfer, task.id)
                    if task.stage == "cache" and isinstance(exc, AdapterError):
                        transition(session, task, "WAITING_RESOURCE", "cache", error=code[:100])
                        task.wait_reason = "AUTH" if "AUTH" in code else "CONNECTION"
                        current.state, current.due = "READY", time.time() + 300
                        current.owner, current.lease_until = "", None
                        return
                    if task.stage == "cache" and transfer and transfer.attempts < 5:
                        transition(session, task, "WAITING_RETRY", "cache", error=code[:100])
                        current.state, current.due = "READY", time.time() + min(3600, 30 * 2**transfer.attempts)
                        current.owner, current.lease_until = "", None
                        return
                    transition(session, task, "FAILED", task.stage, error=code[:100])
                    from app.persistence.models import Identity
                    session.merge(Identity(key=task.content_key, task_id=task.id, result="failed"))
                    current.state = "FAILED"
                elif job.kind in {"document", "postprocess", "document_copy", "failure_archive"}:
                    current.state = "FAILED"
                    if job.kind == "document_copy":
                        task = session.get(Task, job.payload["task_id"])
                        if task and task.state not in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
                            transition(session, task, "FAILED", task.stage, error=code[:100])
                else:
                    current.state, current.due = "READY", time.time() + 300
                current.owner, current.lease_until = "", None
        finally:
            heartbeat.cancel()
            if work:
                work.cancel()
            await asyncio.gather(heartbeat, *([work] if work else []), return_exceptions=True)

    async def poll_job(self, job):
        await self.offline.poll(job, manual=job.payload.get("manual", False))
        return None

    async def notify(self, job):
        with self.db.session() as session:
            self.store.owned(session, job)
            notifications = list(session.scalars(select(Outbox).where(
                Outbox.status == "pending", Outbox.next_run <= time.time(),
            ).limit(20)))
        settings = self.db.settings()
        for item in notifications:
            with self.db.session() as session:
                event = session.get(Event, item.event_id)
                task = session.get(Task, event.task_id)
                offline = session.get(Offline, event.task_id) if item.category == "offline" else None
                task_name = task.name if task else offline.name if offline else ""
                if offline and task_name == "Magnet":
                    task_name = "未命名磁链"
                if not notification_enabled(settings, event, item.category):
                    session.get(Outbox, item.id).status = "suppressed"
                    continue
            webhook = settings["notification_webhook"]
            if not webhook:
                with self.db.session() as session:
                    session.get(Outbox, item.id).status = "suppressed"
                continue
            # Webhook destination must pass validation at configuration time.
            try:
                provider = settings["notification_provider"]
                webhook = normalize_ms_url(webhook) if provider == "ms" else webhook
                parsed = urlsplit(webhook)
                if (parsed.scheme not in {"http", "https"}
                    or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                    or ((self.config.allowed_webhook_hosts or not self.config.local_no_auth)
                        and parsed.hostname not in self.config.allowed_webhook_hosts)):
                    raise ValueError("WEBHOOK_HOST_NOT_AUTHORIZED")
                addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname,
                                                     parsed.port or (443 if parsed.scheme == "https" else 80),
                                                     type=socket.SOCK_STREAM)
                async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False,
                    transport=SafeHTTPTransport(parsed.hostname, list(dict.fromkeys(
                        address[4][0] for address in addresses
                    )))) as client:
                    headers = {}
                    if provider == "ms" and settings.get("notification_api_key"):
                        headers["apiKey"] = self.secrets.decrypt(settings["notification_api_key"])
                    from app.services.notifications import webhook_payload
                    payload = (ms_payload(settings, event, item.category, task_name)
                               if provider == "ms" else webhook_payload(
                                   settings, event, item.category, task_name,
                                   task.state if task else "",
                               ))
                    response = await client.post(webhook, json=payload, headers=headers)
                    response.raise_for_status()
            except Exception as exc:
                with self.db.session() as session:
                    current = session.get(Outbox, item.id)
                    current.attempts += 1
                    current.error = (
                        f"通知服务返回 HTTP {exc.response.status_code}"
                        if isinstance(exc, httpx.HTTPStatusError) else
                        "通知服务连接超时" if isinstance(exc, httpx.TimeoutException) else
                        "通知地址未被允许" if isinstance(exc, ValueError) else
                        "通知发送失败：" + type(exc).__name__
                    )
                    current.next_run = time.time() + current.attempts
                    if current.attempts >= 3:
                        current.status = "failed"
            else:
                with self.db.session() as session:
                    session.get(Outbox, item.id).status = "sent"
        return None

    async def run(self):
        while not self.stopping.is_set():
            self.plan()
            self.start_watchers()
            # Pausing blocks new workflows but permits remote-operation observation.
            paused = self.db.settings()["paused"]
            all_kinds = [
                "offline_submit", "offline_poll", "cloud_scan", "cloud_extract",
                "local_scan", "local_extract", "document", "notifications",
                "postprocess", "cache_gc", "document_copy", "failure_archive",
                "life_monitor",
            ]
            kinds = ["notifications", "cache_gc"] if paused else all_kinds
            active_jobs = None
            if paused:
                with self.db.session() as session:
                    active_jobs = [j.id for j in session.scalars(select(Job).where(
                        Job.kind == "cloud_extract", Job.state == "READY",
                    )) if (t := session.get(Task, j.payload.get("task_id"))) and
                        t.stage in {"cloud_progress", "cloud_verify", "cloud_inspect"}]
                if active_jobs:
                    kinds.append("cloud_extract")
            busy = {j.kind for j, _ in self.running_jobs.values()}
            local_count = sum(j.kind == "local_extract" for j, _ in self.running_jobs.values())
            local_limit = self.db.settings().get("local_extract_concurrency", 1)
            kinds = [kind for kind in kinds if
                     (local_count < local_limit if kind == "local_extract" else kind not in busy)]
            if len(self.running_jobs) >= 3 + local_limit:
                kinds = []
            job = self.store.claim(time.time(), kinds, active_jobs if paused else None)
            if job:
                worker = asyncio.create_task(self.execute(job), name="job-" + job.id)
                self.running_jobs[job.id] = (job, worker)
                def completed(_, job_id=job.id):
                    if not _.cancelled():
                        _.exception()
                    self.running_jobs.pop(job_id, None)
                    self.wake.set()
                worker.add_done_callback(completed)
                continue
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.config.tick_seconds)
            except TimeoutError:
                pass

    def start(self):
        self.initialize()
        if self.config.scheduler_enabled:
            self.runner = asyncio.create_task(self.run(), name="115work-scheduler")

    def start_watchers(self):
        if not self.config.watcher_enabled:
            return
        with self.db.session() as session:
            sources = list(session.scalars(select(Source).where(
                Source.enabled, Source.kind.in_(["local", "inbox"]),
            )))
        for source in sources:
            if source.id not in self.watchers:
                self.watchers[source.id] = asyncio.create_task(self.watch(source.id, source.config["path"]))

    async def watch(self, source_id, path):
        from watchfiles import awatch
        try:
            async for _ in awatch(path, debounce=2000, step=500):
                with self.db.session() as session:
                    source = session.get(Source, source_id)
                    if source and source.enabled:
                        enqueue(session, "scan-source:" + source_id, "local_scan", {"source_id": source_id})
                self.wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("source=%s error=WATCHER_UNAVAILABLE", source_id)

    async def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        workers = [worker for _, worker in self.running_jobs.values()]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await self.local.stop()
        for watcher in self.watchers.values():
            watcher.cancel()
        await asyncio.gather(*self.watchers.values(), return_exceptions=True)
