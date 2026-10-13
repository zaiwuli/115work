import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.adapters.p115 import AdapterError
from app.persistence.models import Offline, Settings, Source
from app.scheduler.store import enqueue
from app.services.events import record
from app.services.folders import durable_folder


PROTECTED_KEYS = ("cloud_output_cid", "cloud_archive_cid", "cloud_failed_cid")


async def initialize_root(state, root):
    if state.config.adapter != "p115":
        raise ValueError("115_CONNECTION_REQUIRED")
    if state.engine.running_jobs:
        raise ValueError("CLOUD_CONFIGURATION_BUSY")
    directories = {}
    for key, name in (
        ("offline_root_cid", "离线目录"), ("cloud_output_cid", "解压目录"),
        ("cloud_archive_cid", "归档目录"), ("cloud_failed_cid", "失败目录"),
    ):
        directories[key] = await durable_folder(
            state.db, state.engine.adapter, root, name, f"managed-root:{root}:{key}",
        )
    state.db.set_settings({"cloud_root_cid": root, "offline_extract_cid": "",
                           **directories})
    with state.db.session() as session:
        record(session, "system", "CLOUD_ROOT_INITIALIZED", "cloud_monitor",
               "云根目录初始化完成；" + "；".join(f"{key}={cid}" for key, cid in directories.items()))
    return directories


class CloudMonitor:
    def __init__(self, db, adapter):
        self.db, self.adapter = db, adapter

    def schedule(self, session, now):
        settings = self.db.settings()
        if not settings["life_monitor_enabled"]:
            return
        saved = session.get(Settings, "life-monitor-state")
        if saved is None or saved.value.get("next", 0) <= now:
            enqueue(session, "life-monitor", "life_monitor", {})

    async def run(self, job):
        settings = self.db.settings()
        if not settings["life_monitor_enabled"]:
            return None
        with self.db.session() as session:
            saved = session.get(Settings, "life-monitor-state")
            checkpoint = saved.value if saved else {}
            sources = list(session.scalars(select(Source).where(Source.kind == "115", Source.enabled)))
            offline = list(session.scalars(select(Offline).where(
                Offline.status.not_in(["failed", "submit_failed", "canceled"]),
                Offline.target_cid != "",
            )))
        now = time.time()
        started = settings.get("life_monitor_started") or now
        since = max(started, checkpoint.get("checked", started))
        first = datetime.fromtimestamp(since, ZoneInfo("Asia/Shanghai")).date()
        today = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date()
        if (today - first).days > 7:
            raise AdapterError("LIFE_BACKLOG_REQUIRES_REVIEW")
        cursor = int(checkpoint.get("cursor", 0))
        newest, events = cursor, []
        day = first
        while day <= today:
            offset = 0
            for _ in range(10):
                response = await self.adapter.call("life_behavior_detail", {
                    "date": str(day), "limit": 1000, "offset": offset,
                })
                data = response.get("data", {})
                rows = data.get("list")
                if not isinstance(rows, list):
                    raise AdapterError("INVALID_LIFE_RESPONSE")
                for event in rows:
                    identifier = int(event["id"])
                    newest = max(newest, identifier)
                    if identifier > cursor and int(event.get("update_time", 0)) >= started:
                        events.append(event)
                offset += len(rows)
                if not rows or offset >= int(data.get("count", offset)):
                    break
            else:
                raise AdapterError("LIFE_BACKLOG_REQUIRES_REVIEW")
            day += timedelta(days=1)
        targets = {}
        protected = {settings[key] for key in PROTECTED_KEYS if settings.get(key)}
        for event in events:
            if int(event.get("type", 0)) not in {1, 2, 5, 6, 14, 17, 18, 23}:
                continue
            parent = str(event.get("parent_id") or "")
            if not parent or parent in protected:
                continue
            if parent in targets:
                continue
            # Validate ancestry without downloading a file or opening a web session.
            path = await self.adapter.directory_path(parent)
            ancestors = set(path)
            if ancestors & protected:
                continue
            matched = [source for source in sources if source.config["cid"] in ancestors]
            if len(matched) > 1:
                matched.sort(key=lambda source: path.index(source.config["cid"]), reverse=True)
            relevant_offline = [item for item in offline if item.target_cid in ancestors]
            if matched:
                targets[parent] = {"cid": parent, "offline_only": False,
                                   "source_id": matched[0].id, "one_shot": True,
                                   "reason": "生活事件：文件移入或新增"}
            elif relevant_offline:
                for item in relevant_offline:
                    targets[item.target_cid] = {
                        "cid": item.target_cid, "offline_only": True, "one_shot": True,
                        "reason": "生活事件：离线目录变化",
                    }
        with self.db.session() as session:
            for cid, payload in targets.items():
                enqueue(session, "scan-cid:" + cid, "cloud_scan", payload)
            session.merge(Settings(key="life-monitor-state", value={
                "cursor": newest, "checked": now, "next": now + 1800,
            }))
            record(session, "system", "LIFE_CHECKED", "cloud_monitor",
                   f"生活事件检查完成：增量事件 {len(events)} 条，局部扫描 {len(targets)} 个目录")
        return None
