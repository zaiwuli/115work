import asyncio
import base64
import hashlib
import io
import json
import secrets
import sqlite3
import time
import zipfile
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.api.auth import authenticate, password_hash
from app.domain.rules import evaluate
from app.persistence.models import Connection, Event, Identity, Rule, Schedule, Settings, Source, Task
from app.scheduler.plans import next_run
from app.scheduler.store import enqueue


router = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticate)])


def row(model):
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}


def audit(request, session, action, target, details=None):
    return None


class ConfigCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    config: dict
    enabled: bool = True
    source_id: str = ""
    expected_revision: int | None = None


def validate_rule(config):
    allowed = {"pattern", "extensions", "min_size", "max_size", "workflow", "original_action",
               "document_action", "document_links", "auto_fallback", "archive_cid", "archive_path",
               "exclude", "output_date", "cloud_failed_cid", "disable_recursion",
               "recurse_iso", "squash_root", "extract_isos",
               "cleanup_enabled", "cleanup_suffixes", "cleanup_recursive"}
    from app.services.output_cleanup import suffixes
    if "cleanup_suffixes" in config:
        config["cleanup_suffixes"] = suffixes(config["cleanup_suffixes"])
    if config.get("cleanup_enabled") and not config.get("cleanup_suffixes"):
        raise ValueError("INVALID_CLEANUP_SUFFIXES")
    for key in ("cleanup_enabled", "cleanup_recursive"):
        if key in config and type(config[key]) is not bool:
            raise ValueError("INVALID_RULE_BOOLEAN")
    if set(config) - allowed:
        raise ValueError("UNKNOWN_RULE_SETTING")
    if config.get("original_action", "retain") not in {"retain", "archive", "delete"}:
        raise ValueError("INVALID_RETENTION_ACTION")
    if config.get("document_action", "register") not in {"register", "copy", "archive"}:
        raise ValueError("INVALID_DOCUMENT_ACTION")
    if config.get("workflow", "auto") not in {"auto", "local", "cloud", "ignore"}:
        raise ValueError("INVALID_WORKFLOW")
    if config.get("original_action") == "delete":
        raise ValueError("DELETION_REQUIRES_PER_TASK_APPROVAL")
    if config.get("extensions") and (not isinstance(config["extensions"], list) or
                                    any(not isinstance(x, str) or not x.startswith(".")
                                        for x in config["extensions"])):
        raise ValueError("INVALID_EXTENSIONS")
    if not isinstance(config.get("pattern", "*"), str) or len(config.get("pattern", "*")) > 1024:
        raise ValueError("INVALID_RULE_PATTERN")
    if "exclude" in config and (not isinstance(config["exclude"], list) or
        len(config["exclude"]) > 100 or any(not isinstance(pattern, str) or len(pattern) > 1024
                                            for pattern in config["exclude"])):
        raise ValueError("INVALID_RULE_EXCLUSIONS")
    for field in ("min_size", "max_size"):
        if field in config and (type(config[field]) is not int or not 0 <= config[field] <= 2**63 - 1):
            raise ValueError("INVALID_RULE_SIZE")
    if config.get("min_size", 0) > config.get("max_size", 2**63 - 1):
        raise ValueError("INVALID_RULE_SIZE_RANGE")
    for field in ("auto_fallback", "output_date", "document_links", "disable_recursion",
                  "recurse_iso", "squash_root", "extract_isos"):
        if field in config and type(config[field]) is not bool:
            raise ValueError("INVALID_RULE_BOOLEAN")
    for field in ("archive_cid", "cloud_failed_cid"):
        if field in config and (not isinstance(config[field], str) or not config[field].isdigit()):
            raise ValueError("INVALID_RULE_CID")
    if "archive_path" in config and (
        not isinstance(config["archive_path"], str) or
        not PurePosixPath(config["archive_path"]).is_absolute() or
        ".." in PurePosixPath(config["archive_path"]).parts
    ):
        raise ValueError("INVALID_ARCHIVE_PATH")


@router.get("/rules")
def rules(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [row(x) for x in session.scalars(select(Rule))]}


@router.post("/rules", status_code=201)
def add_rule(request: Request, body: ConfigCommand):
    validate_rule(body.config)
    with request.app.state.db.session() as session:
        rule = Rule(name=body.name, source_id=body.source_id, config=body.config, enabled=body.enabled)
        session.add(rule)
        session.flush()
        audit(request, session, "RULE_CREATE", rule.id)
        return row(rule)


@router.put("/rules/{rule_id}")
def update_rule(request: Request, rule_id: str, body: ConfigCommand):
    validate_rule(body.config)
    with request.app.state.db.session() as session:
        rule = session.get(Rule, rule_id)
        if not rule:
            raise HTTPException(404, detail="RULE_NOT_FOUND")
        if body.expected_revision != rule.revision:
            raise HTTPException(409, detail="RULE_REVISION_CONFLICT")
        rule.name, rule.config, rule.source_id, rule.enabled = (
            body.name, body.config, body.source_id, body.enabled,
        )
        rule.revision += 1
        audit(request, session, "RULE_UPDATE", rule.id)
        return row(rule)


@router.post("/rules/preview")
async def rule_preview(request: Request):
    body = await request.json()
    with request.app.state.db.session() as session:
        return {"policy": evaluate(session, body.get("source_id", ""), body["name"],
                                    int(body.get("size", 0))), "dry_run": True}


@router.get("/schedules")
def schedules(request: Request):
    raise HTTPException(410, detail="FEATURE_REMOVED")


@router.post("/schedules", status_code=201)
async def add_schedule(request: Request):
    raise HTTPException(410, detail="FEATURE_REMOVED")
    body = await request.json()
    allowed = {"name", "source_id", "trigger", "config", "timezone", "misfire", "enabled"}
    if set(body) - allowed or body.get("misfire", "coalesce") not in {"coalesce", "skip"}:
        raise ValueError("INVALID_SCHEDULE_FIELDS")
    due = next_run(body.get("trigger", "interval"), body.get("config", {}),
                   body.get("timezone", "Asia/Shanghai"), time.time())
    with request.app.state.db.session() as session:
        source_id = body.get("source_id", "")
        if source_id and not session.get(Source, source_id):
            raise ValueError("SOURCE_NOT_FOUND")
        source = session.get(Source, source_id) if source_id else None
        if source and source.kind == "115" and body.get("trigger") == "interval" and body["config"]["seconds"] < 1800:
            raise ValueError("CLOUD_SCAN_MINIMUM_1800_SECONDS")
        schedule = Schedule(**body, next_run=due)
        session.add(schedule)
        session.flush()
        audit(request, session, "SCHEDULE_CREATE", schedule.id)
        return row(schedule)


@router.patch("/schedules/{schedule_id}")
async def change_schedule(request: Request, schedule_id: str):
    raise HTTPException(410, detail="FEATURE_REMOVED")
    body = await request.json()
    with request.app.state.db.session() as session:
        schedule = session.get(Schedule, schedule_id)
        if not schedule:
            raise HTTPException(404, detail="SCHEDULE_NOT_FOUND")
        if body.get("expected_revision") != schedule.revision:
            raise HTTPException(409, detail="SCHEDULE_REVISION_CONFLICT")
        if set(body) - {"enabled", "expected_revision"} or type(body.get("enabled")) is not bool:
            raise ValueError("INVALID_SCHEDULE_UPDATE")
        schedule.enabled = body["enabled"]
        schedule.revision += 1
        schedule.next_run = next_run(schedule.trigger, schedule.config, schedule.timezone, time.time())
        audit(request, session, "SCHEDULE_UPDATE", schedule.id)
        return row(schedule)


@router.put("/schedules/{schedule_id}")
async def edit_schedule(request: Request, schedule_id: str):
    raise HTTPException(410, detail="FEATURE_REMOVED")
    body = await request.json()
    allowed = {"name", "source_id", "trigger", "config", "timezone", "misfire", "enabled",
               "expected_revision"}
    if set(body) - allowed or body.get("misfire", "coalesce") not in {"coalesce", "skip"}:
        raise ValueError("INVALID_SCHEDULE_FIELDS")
    if not isinstance(body.get("name"), str) or not 1 <= len(body["name"]) <= 100:
        raise ValueError("INVALID_SCHEDULE_NAME")
    due = next_run(body["trigger"], body["config"], body.get("timezone", "Asia/Shanghai"), time.time())
    with request.app.state.db.session() as session:
        saved = session.get(Schedule, schedule_id)
        if not saved:
            raise HTTPException(404, detail="SCHEDULE_NOT_FOUND")
        if saved.revision != body.get("expected_revision"):
            raise HTTPException(409, detail="SCHEDULE_REVISION_CONFLICT")
        source_id = body.get("source_id", "")
        source = session.get(Source, source_id) if source_id else None
        if source_id and not source:
            raise ValueError("SOURCE_NOT_FOUND")
        if (source and source.kind == "115" and body["trigger"] == "interval" and
                body["config"]["seconds"] < 1800):
            raise ValueError("CLOUD_SCAN_MINIMUM_1800_SECONDS")
        for key, value in body.items():
            if key != "expected_revision":
                setattr(saved, key, value)
        saved.revision += 1
        saved.next_run = due
        audit(request, session, "SCHEDULE_UPDATE", saved.id)
        return row(saved)


@router.post("/schedules/{schedule_id}/run", status_code=202)
def run_schedule(request: Request, schedule_id: str):
    raise HTTPException(410, detail="FEATURE_REMOVED")
    with request.app.state.db.session() as session:
        schedule = session.get(Schedule, schedule_id)
        if not schedule:
            raise HTTPException(404, detail="SCHEDULE_NOT_FOUND")
        schedule.next_run = time.time()
        audit(request, session, "SCHEDULE_RUN", schedule.id)
    request.app.state.engine.wake.set()
    return {"operation_id": schedule_id}


@router.get("/connections")
def connections(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [{k: v for k, v in row(x).items() if k != "credential"} |
                          {"credential_configured": bool(x.credential)}
                         for x in session.scalars(select(Connection))]}


@router.post("/connections", status_code=201)
async def add_connection(request: Request):
    body = await request.json()
    if set(body) - {"name", "kind", "config", "credential", "enabled"}:
        raise ValueError("INVALID_CONNECTION_FIELDS")
    kind = body.get("kind")
    if kind != "115":
        raise ValueError("INVALID_CONNECTION_KIND")
    credential = body.pop("credential", "")
    if not credential:
        raise ValueError("CONNECTION_CREDENTIAL_REQUIRED")
    with request.app.state.db.session() as session:
        if kind == "115" and session.scalar(select(Connection).where(Connection.kind == "115")):
            raise HTTPException(409, detail="SINGLE_115_ACCOUNT_ONLY")
        connection = Connection(**body, credential=request.app.state.secrets.encrypt(credential))
        session.add(connection)
        session.flush()
        audit(request, session, "CONNECTION_CREATE", connection.id)
        identifier = connection.id
    activated = await activate_connection(request, identifier)
    return {"id": identifier, "credential_configured": True, **activated}


@router.post("/connections/{connection_id}/credentials")
async def rotate_credentials(request: Request, connection_id: str):
    body = await request.json()
    with request.app.state.db.session() as session:
        connection = session.get(Connection, connection_id)
        if not connection:
            raise HTTPException(404, detail="CONNECTION_NOT_FOUND")
        if body.get("expected_revision") != connection.revision:
            raise HTTPException(409, detail="CONNECTION_REVISION_CONFLICT")
        if not body.get("credential"):
            raise ValueError("CONNECTION_CREDENTIAL_REQUIRED")
        connection.credential = request.app.state.secrets.encrypt(body["credential"])
        connection.revision += 1
        connection.health = "UNKNOWN"
        audit(request, session, "CREDENTIAL_ROTATE", connection.id)
    return await activate_connection(request, connection_id)


@router.delete("/connections/{connection_id}")
async def delete_connection(request: Request, connection_id: str):
    state = request.app.state
    if not hasattr(state, "connection_activation_lock"):
        state.connection_activation_lock = asyncio.Lock()
    async with state.connection_activation_lock:
        with state.db.session() as session:
            connection = session.get(Connection, connection_id)
            if not connection:
                raise HTTPException(404, detail="CONNECTION_NOT_FOUND")
            if state.engine.running_jobs:
                raise HTTPException(409, detail="CONNECTION_DELETE_BUSY")
            session.delete(connection)
        # No await between the busy check and adapter replacement: workers cannot
        # start using the old credentials while this connection is removed.
        from app.adapters.p115 import DisabledAdapter
        selected = DisabledAdapter()
        engine = state.engine
        previous = engine.adapter
        engine.adapter = engine.offline.adapter = engine.archives.adapter = selected
        engine.retention.adapter = engine.local.transfer.adapter = selected
        engine.monitor.adapter = selected
        engine.offline.snapshot = None
        state.config.adapter = "disabled"
        state.db.set_settings({"adapter_mode": "disabled"})
        close = getattr(previous, "close", None)
        try:
            if close:
                await close()
        finally:
            engine.wake.set()
    return {"success": True}


@router.post("/connections/{connection_id}/diagnose")
async def diagnose_connection(request: Request, connection_id: str):
    with request.app.state.db.session() as session:
        connection = session.get(Connection, connection_id)
        if not connection:
            raise HTTPException(404, detail="CONNECTION_NOT_FOUND")
    if connection.kind == "115":
        from app.adapters.p115 import P115Adapter
        try:
            selected = P115Adapter(request.app.state.secrets.decrypt(connection.credential),
                                   request.app.state.config.remote_timeout)
            try:
                await selected.files(connection.config.get("root_cid", "0"))
            finally:
                await selected.close()
            capabilities, health = {"offline": True, "extract": True}, "MATCHED"
        except Exception:
            capabilities, health = {}, "AUTH_OR_CONNECTION_REQUIRED"
    with request.app.state.db.session() as session:
        current = session.get(Connection, connection_id)
        current.health, current.capabilities = health, capabilities
        audit(request, session, "CONNECTION_DIAGNOSE", connection_id)
    return {"status": health, "capabilities": capabilities}


@router.post("/connections/{connection_id}/activate")
async def activate_connection(request: Request, connection_id: str):
    state = request.app.state
    if not hasattr(state, "connection_activation_lock"):
        state.connection_activation_lock = asyncio.Lock()
    async with state.connection_activation_lock:
        paused = state.db.settings()["paused"]
        state.db.set_settings({"paused": True})
        try:
            # Keep the old adapter alive until its in-flight jobs have completed.
            deadline = time.monotonic() + 60
            while state.engine.running_jobs:
                if time.monotonic() >= deadline:
                    raise HTTPException(409, detail="CONNECTION_SWITCH_BUSY")
                await asyncio.sleep(0.2)
            return await _activate_connection(request, connection_id)
        finally:
            state.db.set_settings({"paused": paused})
            state.engine.wake.set()


async def _activate_connection(request: Request, connection_id: str):
    state = request.app.state
    engine = state.engine
    with state.db.session() as session:
        connection = session.get(Connection, connection_id)
        if not connection or connection.kind != "115":
            raise ValueError("115_CONNECTION_REQUIRED")
        cookie = state.secrets.decrypt(connection.credential)
    from app.adapters.p115 import P115Adapter
    from app.scheduler.governor import RemoteGovernor
    selected = P115Adapter(cookie, state.config.remote_timeout)
    selected.governor = RemoteGovernor(state.db)
    selected.governor.success()
    previous = engine.adapter
    engine.adapter = engine.offline.adapter = engine.archives.adapter = selected
    engine.retention.adapter = engine.local.transfer.adapter = selected
    engine.monitor.adapter = selected
    engine.offline.snapshot = None
    state.config.adapter = "p115"
    state.db.set_settings({"adapter_mode": "p115"})
    close = getattr(previous, "close", None)
    if close:
        await close()
    with state.db.session() as session:
        audit(request, session, "115_ACTIVATE", connection_id)
        from app.persistence.models import Job
        for job in session.scalars(select(Job).where(
            Job.state == "READY", Job.error == "AUTH_REQUIRED",
        )):
            job.due, job.error = time.time(), ""
    return {"success": True, "application": "activated"}


@router.post("/connections/{connection_id}/browse")
async def browse_connection(request: Request, connection_id: str):
    body = await request.json()
    with request.app.state.db.session() as session:
        connection = session.get(Connection, connection_id)
        if not connection:
            raise HTTPException(404, detail="CONNECTION_NOT_FOUND")
    if connection.kind == "115":
        from dataclasses import asdict
        page = await request.app.state.engine.adapter.files(body.get("cid", "0"), body.get("offset", 0))
        return {"items": [asdict(x) for x in page.files], "next_offset": page.next_offset}
    raise HTTPException(422, detail="INVALID_CONNECTION_KIND")


@router.post("/security/tokens", status_code=201)
async def create_token(request: Request):
    body = await request.json()
    if body.get("role") not in {"readonly", "import"}:
        raise ValueError("INVALID_TOKEN_ROLE")
    value = secrets.token_urlsafe(32)
    key = "access-token:" + hashlib.sha256(value.encode()).hexdigest()
    ttl = body.get("ttl_days", 30)
    if type(ttl) is not int or not 1 <= ttl <= 365:
        raise ValueError("INVALID_TOKEN_EXPIRY")
    with request.app.state.db.session() as session:
        session.add(Settings(key=key, value={"role": body["role"], "name": str(body.get("name", body["role"]))[:100],
                                           "expires": time.time() + ttl * 86400}))
        audit(request, session, "TOKEN_CREATE", key, {"role": body["role"]})
    return {"token": value, "id": key, "role": body["role"], "shown_once": True}


@router.get("/security/tokens")
def tokens(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [{"id": x.key, **x.value} for x in session.scalars(
            select(Settings).where(Settings.key.startswith("access-token:")))]}


@router.get("/import/batches/{batch_id}")
def own_batch(request: Request, batch_id: str):
    from app.persistence.models import BatchItem, Offline
    with request.app.state.db.session() as session:
        owner = session.get(Settings, "batch-owner:" + batch_id)
        if not owner or owner.value["token"] != getattr(request.state, "token_key", ""):
            raise HTTPException(404, detail="BATCH_NOT_FOUND")
        return {"batch_id": batch_id, "items": [
            {"id": x.id, "name": x.name, "status": x.status, "stage": x.stage}
            for x in session.scalars(select(Offline).join(BatchItem, BatchItem.task_id == Offline.id)
                                    .where(BatchItem.batch_id == batch_id))
        ]}


@router.delete("/security/tokens/{token_id}")
def revoke_token(request: Request, token_id: str):
    with request.app.state.db.session() as session:
        saved = session.get(Settings, token_id)
        if not saved or not token_id.startswith("access-token:"):
            raise HTTPException(404, detail="TOKEN_NOT_FOUND")
        session.delete(saved)
        audit(request, session, "TOKEN_REVOKE", token_id)
    return {"success": True}


@router.post("/security/password")
async def change_admin_password(request: Request):
    body = await request.json()
    if not isinstance(body.get("password"), str) or not 12 <= len(body["password"]) <= 1024:
        raise ValueError("PASSWORD_REQUIRES_12_CHARACTERS")
    with request.app.state.db.session() as session:
        session.merge(Settings(key="admin_password", value={"value": password_hash(body["password"])}))
        session.execute(delete(Settings).where(Settings.key.startswith("session:")))
        audit(request, session, "ADMIN_PASSWORD_CHANGED", "admin")
    return {"success": True}


@router.get("/maintenance/cache")
def cache_preview(request: Request):
    return {"items": request.app.state.engine.retention.cache_preview()}


@router.post("/tasks/{task_id}/postprocess/retry", status_code=202)
def retry_postprocess(request: Request, task_id: str):
    with request.app.state.db.session() as session:
        task = session.get(Task, task_id)
        if not task or task.state != "SUCCEEDED":
            raise HTTPException(409, detail="PROCESSING_SUCCESS_REQUIRED")
        enqueue(session, "postprocess:" + task_id, "postprocess", {"task_id": task_id})
        audit(request, session, "POSTPROCESS_RETRY", task_id)
    request.app.state.engine.wake.set()
    return {"operation_id": task_id}


@router.post("/tasks/{task_id}/delete-approval", status_code=202)
async def approve_delete(request: Request, task_id: str):
    body = await request.json()
    with request.app.state.db.session() as session:
        task = session.get(Task, task_id)
        if not task or task.state != "SUCCEEDED" or body.get("confirm_name") != task.name:
            raise HTTPException(409, detail="DELETE_CONFIRMATION_REQUIRED")
        task.policy = task.policy | {"original_action": "delete", "delete_approved": True}
        enqueue(session, "postprocess:" + task_id, "postprocess", {"task_id": task_id},
                due=max(time.time(), task.completed_at + request.app.state.config.delete_delay_seconds))
        audit(request, session, "DELETE_APPROVED", task_id)
    return {"operation_id": task_id, "delay_seconds": request.app.state.config.delete_delay_seconds}


@router.get("/logs")
def logs(request: Request, after: int = 0, page_size: int = 100):
    with request.app.state.db.session() as session:
        items = []
        for event in session.scalars(select(Event).where(Event.id > after)
                .order_by(Event.id.desc()).limit(min(200, max(1, page_size)))):
            task = session.get(Task, event.task_id)
            details = {"关联编号": event.task_id, "工作记录": event.message}
            if task:
                details.update({
                    "文件名": task.name, "文件大小（字节）": task.size,
                    "输入路径或文件ID": task.locator.get("path") or task.locator.get("fid"),
                    "输入目录CID": task.locator.get("cid", ""),
                    "输出路径或CID": task.output.get("path") or task.output.get("cid", ""),
                    "当前错误代码": task.error or "无",
                    "尝试次数": task.attempts, "状态": task.state,
                    "校验方式": task.output.get("verification", ""),
                    "创建至完成耗时（秒）": round(task.completed_at - task.created_at, 2)
                        if task.completed_at else "未完成",
                    "输出文件数量": len(task.output.get("actual_manifest", [])),
                })
            items.append(row(event) | {"details": details})
        return {"items": items}


@router.get("/audit")
def audits(request: Request, page: int = 1):
    raise HTTPException(410, detail="FEATURE_REMOVED")


@router.post("/cloud/root")
async def cloud_root(request: Request):
    body = await request.json()
    cid = body.get("cid")
    if not isinstance(cid, str) or not cid.isdigit():
        raise HTTPException(422, detail="INVALID_CID")
    from app.services.cloud_monitor import initialize_root
    return await initialize_root(request.app.state, cid)


@router.post("/cloud/monitor/check", status_code=202)
def monitor_check(request: Request):
    if not request.app.state.db.settings()["life_monitor_enabled"]:
        raise HTTPException(409, detail="LIFE_MONITOR_DISABLED")
    with request.app.state.db.session() as session:
        enqueue(session, "life-monitor", "life_monitor", {"manual": True})
    request.app.state.engine.wake.set()
    return {"success": True}


@router.delete("/rules/{rule_id}")
def remove_rule(request: Request, rule_id: str):
    with request.app.state.db.session() as session:
        rule = session.get(Rule, rule_id)
        if not rule:
            raise HTTPException(404, detail="RULE_NOT_FOUND")
        session.delete(rule)
    return {"success": True}


@router.delete("/tasks/{task_id}")
def remove_task(request: Request, task_id: str):
    from app.services.deletion import delete_task
    with request.app.state.db.session() as session:
        try:
            delete_task(session, task_id)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from None
    return {"success": True}


@router.delete("/offline/{task_id}")
def remove_offline(request: Request, task_id: str):
    from app.services.deletion import delete_offline
    with request.app.state.db.session() as session:
        try:
            delete_offline(session, task_id)
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from None
    return {"success": True}


@router.get("/system/capabilities")
def capabilities(request: Request):
    from app.adapters.extract_worker import extractor_available
    from app.services.archives import ARCHIVE_SUFFIXES
    available = extractor_available(request.app.state.config)
    return {"extractor": available, "extract_engine": request.app.state.config.extract_engine,
            "formats": list(ARCHIVE_SUFFIXES) if available else [".zip", ".tar"],
            "pdf_docx": True,
            "remote_mode": request.app.state.config.adapter}


@router.get("/system/metrics")
def metrics(request: Request):
    from sqlalchemy import func
    from app.persistence.models import CacheEntry, Job, Outbox
    db = request.app.state.db
    with db.session() as session:
        circuit = session.get(Settings, "circuit:115-main")
        return {"remote": getattr(request.app.state.engine.adapter, "metrics", {}),
                "cache_bytes": session.scalar(select(func.sum(CacheEntry.size))) or 0,
                "active_jobs": session.scalar(select(func.count(Job.id)).where(Job.state == "RUNNING")),
                "ready_jobs": session.scalar(select(func.count(Job.id)).where(Job.state == "READY")),
                "pending_notifications": session.scalar(select(func.count(Outbox.id)).where(Outbox.status == "pending")),
                "circuit": circuit.value if circuit else {}}


@router.post("/documents/upload", status_code=202)
async def upload_document(request: Request):
    form = await request.form(max_files=1, max_fields=1)
    upload = form.get("file")
    if not upload or not hasattr(upload, "read"):
        raise ValueError("DOCUMENT_UPLOAD_REQUIRED")
    from pathlib import Path
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in {".txt", ".md", ".csv", ".json", ".pdf", ".docx"}:
        raise ValueError("DOCUMENT_FORMAT_NOT_ENABLED")
    raw = await upload.read(request.app.state.config.max_document_bytes + 1)
    if len(raw) > request.app.state.config.max_document_bytes:
        raise HTTPException(413, detail="DOCUMENT_SIZE_LIMIT")
    config = request.app.state.config
    with request.app.state.db.session() as session:
        source = session.scalar(select(Source).where(Source.kind == "inbox", Source.enabled,
            Source.config["path"].as_string() == str(config.local_path("inbox/links"))))
        if not source:
            raise ValueError("INBOX_SOURCE_REQUIRED")
        path = config.local_path("inbox/links") / (secrets.token_hex(16) + suffix)
        with path.open("xb") as stream:
            stream.write(raw)
        job = enqueue(session, "document:" + str(path), "document",
                      {"source_id": source.id, "path": str(path)})
        session.flush()
        audit(request, session, "DOCUMENT_UPLOAD", path.name)
        operation_id = job.id
    request.app.state.engine.wake.set()
    return {"operation_id": operation_id}


@router.post("/documents/{digest}/retry", status_code=202)
def retry_document(request: Request, digest: str):
    from pathlib import Path
    import shutil
    from app.domain.paths import contained
    from app.persistence.models import DocumentImport
    config = request.app.state.config
    with request.app.state.db.session() as session:
        document = session.get(DocumentImport, digest)
        if not document or document.state != "FAILED":
            raise HTTPException(409, detail="FAILED_DOCUMENT_REQUIRED")
        original = contained(config.local_path("inbox/failed"), Path(document.path))
        if not original.is_file() or original.stat().st_size > config.max_document_bytes:
            raise ValueError("DOCUMENT_SIZE_LIMIT_OR_MISSING")
        source = session.scalar(select(Source).where(
            Source.kind == "inbox", Source.enabled,
            Source.config["path"].as_string() == str(config.local_path("inbox/links")),
        ))
        if not source:
            raise ValueError("INBOX_SOURCE_REQUIRED")
        destination = config.local_path("inbox/links") / (secrets.token_hex(16) + original.suffix)
        with original.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        document.state, document.path, document.error = "PARSED", str(destination), ""
        job = enqueue(session, "document:" + str(destination), "document",
                      {"source_id": source.id, "path": str(destination)})
        session.flush()
        audit(request, session, "DOCUMENT_RETRY", digest)
        identifier = job.id
    request.app.state.engine.wake.set()
    return {"operation_id": identifier}


@router.post("/tasks/{task_id}/reconcile", status_code=202)
async def reconcile_task(request: Request, task_id: str):
    body = await request.json()
    with request.app.state.db.session() as session:
        task = session.get(Task, task_id)
        if not task or task.state != "RECONCILING":
            raise HTTPException(409, detail="RECONCILING_TASK_REQUIRED")
        if task.version != body.get("expected_version"):
            raise HTTPException(409, detail="TASK_VERSION_CONFLICT")
        if task.stage == "cloud_export_unknown":
            if not isinstance(body.get("remote_id"), str) or not body["remote_id"].isdigit():
                raise ValueError("REMOTE_OPERATION_ID_REQUIRED")
            task.remote_id, task.stage, task.state = body["remote_id"], "cloud_progress", "WAITING_EXTERNAL"
            enqueue(session, "archive:" + task_id, "cloud_extract", {"task_id": task_id})
        elif task.stage == "local_extract":
            from app.services.local import LocalService
            from pathlib import Path
            manifest_path = Path(task.output.get("manifest", ""))
            if not manifest_path.is_file():
                raise ValueError("OUTPUT_MANIFEST_REQUIRED")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not await asyncio.to_thread(LocalService.verify_manifest, Path(task.output["path"]), manifest):
                raise ValueError("OUTPUT_MANIFEST_MISMATCH")
            task.state, task.stage = "SUCCEEDED", "complete"
            session.merge(Identity(key=task.content_key, task_id=task.id, result="success"))
        else:
            enqueue(session, "archive:" + task_id, "cloud_extract", {"task_id": task_id})
        audit(request, session, "RECONCILE_REQUEST", task_id)
    request.app.state.engine.wake.set()
    return {"operation_id": task_id}




@router.post("/tasks/{task_id}/failure-archive/reconcile", status_code=202)
async def reconcile_failure_archive(request: Request, task_id: str):
    body = await request.json()
    from app.persistence.models import Operation
    from app.services.execution import operation_scope
    with request.app.state.db.session() as session:
        task = session.get(Task, task_id)
        key = "failed-archive:" + operation_scope(task) if task else ""
        operation = session.get(Operation, key)
        if (not task or not operation or task.source != "115" or
            task.output.get("failure_archive_result") not in {"PENDING", "FAILED"}):
            raise HTTPException(409, detail="FAILED_ARCHIVE_RECONCILIATION_REQUIRED")
        if body.get("expected_version") != task.version or body.get("confirm_name") != task.name:
            raise HTTPException(409, detail="TASK_CONFIRMATION_REQUIRED")
    destination = task.locator | {"cid": operation.evidence["to_cid"]}
    await request.app.state.engine.adapter.verify_locator(destination)
    with request.app.state.db.session() as session:
        current = session.get(Task, task_id)
        if current.version != task.version:
            raise HTTPException(409, detail="TASK_VERSION_CONFLICT")
        current.locator = destination | {"original_cid": operation.evidence["from_cid"]}
        current.output = current.output | {"failure_archive_result": "SUCCEEDED"}
        session.get(Operation, key).status = "COMPLETED"
        audit(request, session, "FAILURE_ARCHIVE_RECONCILED", task_id)
    return {"operation_id": task_id, "status": "verified_in_destination"}


@router.post("/tasks/{task_id}/reprocess", status_code=202)
async def reprocess_task(request: Request, task_id: str):
    body = await request.json()
    from app.persistence.models import Job
    from app.services.events import record
    with request.app.state.db.session() as session:
        previous = session.get(Task, task_id)
        if not previous or previous.state not in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
            raise HTTPException(409, detail="TERMINAL_TASK_REQUIRED")
        if body.get("expected_version") != previous.version or body.get("confirm_name") != previous.name:
            raise HTTPException(409, detail="REPROCESS_CONFIRMATION_REQUIRED")
        active = session.scalar(select(Task).where(Task.object_key == previous.object_key,
            Task.state.not_in(["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"])))
        if active:
            raise HTTPException(409, detail="ACTIVE_RESOURCE_EXECUTION")
        from app.services.archives import is_archive
        document = not is_archive(previous.name) and not previous.locator.get("remote_parts")
        pending = session.scalar(select(Job.id).where(
            Job.payload["task_id"].as_string() == task_id,
            Job.state.in_(["RUNNING", "READY", "RECONCILING"]),
        ))
        if pending:
            raise HTTPException(409, detail="TASK_OPERATIONS_PENDING")
        task = previous
        from app.services.execution import begin_execution
        begin_execution(session, task)
        task.state, task.stage = "READY", (
            "document_copy" if document else "cloud_prepare" if task.source == "115"
            else "local_extract"
        )
        task.output, task.remote_id, task.error = {}, "", ""
        task.password_attempts, task.cancel_requested, task.completed_at = 0, False, None
        task.updated_at = time.time()
        task.policy = task.policy | {"auto_extract": True}
        identity = session.get(Identity, previous.content_key)
        if identity:
            session.delete(identity)
        kind = "document_copy" if document else "cloud_extract" if task.source == "115" else "local_extract"
        enqueue(session, kind + ":" + task.id, kind, {"task_id": task.id})
        record(session, task.id, "REPROCESS_AUTHORIZED", task.stage, "原任务重新进入处理队列")
        audit(request, session, "REPROCESS", task.id, {"previous_task": previous.id})
        identifier = task.id
    request.app.state.engine.wake.set()
    return {"operation_id": identifier}


@router.post("/maintenance/backup")
async def backup(request: Request):
    config = request.app.state.config
    folder = config.data_dir / "config/backups"
    folder.mkdir(exist_ok=True)
    name = f"backup-{time.time_ns()}"
    destination = folder / (name + ".db")
    with sqlite3.connect(config.db_path) as source:
        with sqlite3.connect(destination) as target:
            source.backup(target)
    bundle = folder / (name + ".zip")
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.write(destination, "115work.db")
        output.write(config.data_dir / "config/master.key", "master.key")
        output.writestr("README.txt", "Sensitive encrypted credentials and master key. Keep offline and private.")
    with request.app.state.db.session() as session:
        audit(request, session, "BACKUP_CREATE", name)
    return {"name": bundle.name, "path": str(bundle), "contains_secrets": True}


@router.post("/maintenance/legacy-import")
async def legacy_import(request: Request):
    body = await request.json()
    entries = body.get("records", [])
    if "processed" in body or "failed" in body or "pending_cd2" in body:
        entries = []
        for key, status in (("processed", "success"), ("failed", "failed"),
                            ("ignored", "ignored"), ("pending_cd2", "unknown"),
                            ("pending_115_fallback", "unknown")):
            values = body.get(key, {})
            if not isinstance(values, dict):
                raise ValueError("INVALID_LEGACY_STATE")
            for item in values.values():
                if not isinstance(item, dict):
                    raise ValueError("INVALID_LEGACY_RECORD")
                from pathlib import PureWindowsPath
                path = item.get("path", "")
                name = item.get("file_name") or PureWindowsPath(path).name or item.get("key")
                entries.append({"name": name, "size": item.get("size", 0), "status": status})
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError("LEGACY_IMPORT_LIMIT")
    report = {"imported": 0, "duplicates": 0, "reconciling": 0}
    with request.app.state.db.session() as session:
        if not request.app.state.db.settings()["paused"]:
            raise HTTPException(409, detail="PAUSE_REQUIRED")
        for entry in entries:
            name, size = entry.get("name") or entry.get("file_name"), entry.get("size", 0)
            if not isinstance(name, str) or type(size) is not int or size < 0:
                raise ValueError("INVALID_LEGACY_RECORD")
            from app.domain.links import match_name
            key = f"{match_name(name)}:{size}"
            if session.get(Identity, key):
                report["duplicates"] += 1
                continue
            status = ("SUCCEEDED" if entry.get("status") in {"success", "completed", "SUCCEEDED"}
                      else "FAILED" if entry.get("status") in {"failed", "FAILED"}
                      else "SKIPPED" if entry.get("status") in {"ignored", "SKIPPED"}
                      else "RECONCILING")
            task = Task(object_key="legacy:" + secrets.token_hex(16), content_key=key,
                        source="legacy", name=name, size=size, state=status,
                        stage="legacy_reconcile", locator={}, policy={"retain_original": True},
                        error="LEGACY_NEEDS_RECONCILIATION" if status == "RECONCILING" else "")
            session.add(task)
            session.flush()
            if status in {"SUCCEEDED", "FAILED", "SKIPPED"}:
                session.add(Identity(key=key, task_id=task.id, result=status.lower()))
            else:
                report["reconciling"] += 1
            report["imported"] += 1
        audit(request, session, "LEGACY_IMPORT", "system", report)
    return report


@router.post("/auth/115/qrcode")
async def qrcode_start(request: Request):
    from p115client import P115Client
    try:
        response = await asyncio.wait_for(P115Client.login_qrcode_token(async_=True), 30)
        data = response["data"]
        identifier = secrets.token_hex(16)
        with request.app.state.db.session() as session:
            session.add(Settings(key="qrcode:" + identifier,
                                 value={"encrypted": request.app.state.secrets.encrypt(json.dumps(data)),
                                        "expires": time.time() + 180}))
        import qrcode
        image = qrcode.make(data.get("qrcode") or "https://115.com/scan/dg-" + data["uid"])
        output = io.BytesIO()
        image.save(output, format="PNG")
        return {"id": identifier, "image": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()}
    except Exception:
        raise HTTPException(502, detail="QRCODE_START_FAILED") from None


@router.post("/auth/115/qrcode/{identifier}")
async def qrcode_poll(request: Request, identifier: str):
    from p115client import P115Client
    with request.app.state.db.session() as session:
        saved = session.get(Settings, "qrcode:" + identifier)
        if not saved or saved.value["expires"] < time.time():
            raise HTTPException(410, detail="QRCODE_EXPIRED")
        data = json.loads(request.app.state.secrets.decrypt(saved.value["encrypted"]))
    try:
        response = await asyncio.wait_for(P115Client.login_qrcode_scan_status(
            {k: data[k] for k in ("uid", "time", "sign")}, async_=True,
        ), 30)
        status = response.get("data", {}).get("status", 0)
        if status == 2:
            result = await P115Client.login_qrcode_scan_result(data["uid"], app="os_windows", async_=True)
            cookie = result["data"]["cookie"]
            value = "; ".join(f"{k}={v}" for k, v in cookie.items()) if isinstance(cookie, dict) else str(cookie)
            with request.app.state.db.session() as session:
                connection = session.scalar(select(Connection).where(Connection.kind == "115"))
                if not connection:
                    connection = Connection(name="115", kind="115", config={"root_cid": "0"})
                    session.add(connection)
                connection.credential = request.app.state.secrets.encrypt(value)
                connection.revision += 1
                session.delete(session.get(Settings, "qrcode:" + identifier))
                audit(request, session, "QRCODE_LOGIN", "115")
            return {"status": "confirmed", "application": "restart_required"}
        return {"status": {0: "waiting", 1: "scanned", -1: "expired", -2: "canceled"}.get(status, "unknown")}
    except Exception:
        raise HTTPException(502, detail="QRCODE_POLL_FAILED") from None
