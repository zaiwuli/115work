import asyncio
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.api.auth import authenticate, configured, password_hash, password_matches
from app.api.schemas import ImportRequest, LoginRequest, SourceRequest, TaskCommand
from app.persistence.db import DEFAULT_SETTINGS
from app.persistence.models import (
    Batch, BatchItem, DocumentImport, Event, Identity, Job, Offline, Outbox,
    ResourceLink, Settings, Source, Task,
    Step, Transfer,
    OfflineExecution,
)
from app.scheduler.store import enqueue
from app.services.events import record, transition


router = APIRouter()
secured = APIRouter(dependencies=[Depends(authenticate)])


def serialize(model) -> dict:
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}


@router.get("/api/auth/status")
def auth_status(request: Request):
    logged_in = False
    try:
        authenticate(request)
        logged_in = True
    except HTTPException:
        pass
    csrf = ""
    if logged_in:
        token = request.cookies.get("work115_session", "")
        with request.app.state.db.session() as session:
            saved = session.get(Settings, "session:" + hashlib.sha256(token.encode()).hexdigest())
            if saved:
                csrf = saved.value["csrf"]
    return {"configured": configured(request), "authenticated": logged_in, "csrf_token": csrf,
            "local_no_auth": request.app.state.config.local_no_auth}


@router.post("/api/auth/setup")
def setup(request: Request, body: LoginRequest):
    if configured(request):
        raise HTTPException(409, detail="ALREADY_CONFIGURED")
    if not hmac.compare_digest(
        request.headers.get("authorization", ""), "Bearer " + request.app.state.api_token,
    ):
        raise HTTPException(401, detail="BOOTSTRAP_TOKEN_REQUIRED")
    if len(body.password) < 12:
        raise HTTPException(422, detail="PASSWORD_REQUIRES_12_CHARACTERS")
    try:
        with request.app.state.db.session() as session:
            session.add(Settings(key="admin_password", value={"value": password_hash(body.password)}))
    except IntegrityError:
        raise HTTPException(409, detail="ALREADY_CONFIGURED") from None
    return {"success": True}


@router.post("/api/auth/login")
def login(request: Request, response: Response, body: LoginRequest):
    attempts = request.app.state.login_attempts
    address = request.client.host if request.client else ""
    count, since = attempts.get(address, (0, time.time()))
    if time.time() - since > 300:
        count, since = 0, time.time()
    if count >= 10:
        raise HTTPException(429, detail="LOGIN_RATE_LIMIT")
    attempts[address] = (count + 1, since)
    with request.app.state.db.session() as session:
        saved = session.get(Settings, "admin_password")
        if not saved or not password_matches(body.password, saved.value["value"]):
            raise HTTPException(401, detail="INVALID_LOGIN")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        session.add(Settings(key="session:" + hashlib.sha256(token.encode()).hexdigest(),
                             value={"expires": time.time() + 86400, "csrf": csrf}))
    attempts.pop(address, None)
    response.set_cookie("work115_session", token, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=86400)
    return {"success": True, "csrf_token": csrf}


@secured.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("work115_session", "")
    with request.app.state.db.session() as session:
        session.execute(delete(Settings).where(
            Settings.key == "session:" + hashlib.sha256(token.encode()).hexdigest(),
        ))
    response.delete_cookie("work115_session")
    return {"success": True}


@secured.get("/api/status")
@secured.get("/api/v1/system")
def system_status(request: Request):
    from app.adapters.extract_worker import extractor_available
    db = request.app.state.db
    with db.session() as session:
        tasks = list(session.scalars(select(Task).where(
            Task.state.not_in(["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"]),
        ).order_by(Task.created_at.desc()).limit(100)))
        events = list(session.scalars(select(Event).order_by(Event.id.desc()).limit(30)))
        sources = list(session.scalars(select(Source)))
        total = session.scalar(select(func.count(Task.id)).where(Task.state == "SUCCEEDED"))
        approvals = session.scalar(select(func.count(Task.id)).where(Task.state == "WAITING_APPROVAL"))
        failures = session.scalar(select(func.count(Task.id)).where(Task.state == "FAILED"))
        active = session.scalar(select(func.count(Task.id)).where(
            Task.state.not_in(["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"]),
        ))
    settings = db.settings()
    return {
        "version": "0.1.0", "adapter": request.app.state.config.adapter,
        "role": getattr(request.state, "role", "admin"),
        "local_formats": "ZIP / TAR / 7z / RAR / ISO" if extractor_available(request.app.state.config) else "ZIP / TAR",
        "extract_engine": request.app.state.config.extract_engine,
        "updated_at": time.time(), "paused": settings["paused"],
        "totals": {"active": active, "finished": total, "retries": sum(t.attempts for t in tasks),
                   "workers": 1, "approvals": approvals, "failed": failures},
        "tasks": [{"id": x.id, "name": x.name, "source": x.source, "state": x.state,
                   "stage": x.stage, "attempts": x.attempts} for x in tasks],
        "logs": [serialize(x) for x in events], "sources": [serialize(x) for x in sources],
        "folders": [{"path": x.config.get("path", ""), "tracked": 0,
                     "extract_path": x.config.get("output_path") or str(request.app.state.config.local_path("output"))}
                    for x in sources if x.kind != "115"],
        "passwords": ["********"] * len(settings["passwords"]),
    }


@secured.get("/api/v1/tasks")
def list_tasks(request: Request, state: str = "", source: str = "", q: str = "",
               page: int = 1, page_size: int = 50, terminal: bool = False):
    page, page_size = max(1, page), min(100, max(1, page_size))
    with request.app.state.db.session() as session:
        query = select(Task).where(~Task.hidden)
        if terminal:
            query = query.where(Task.state.in_(["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"]))
        if state == "ACTIVE":
            query = query.where(Task.state.not_in(["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"]))
        elif state:
            query = query.where(Task.state == state)
        if source:
            query = query.where(Task.source == source)
        if q:
            query = query.where(Task.name.contains(q, autoescape=True))
        total = session.scalar(select(func.count()).select_from(query.subquery()))
        items = session.scalars(query.order_by(Task.created_at.desc())
                               .offset((page - 1) * page_size).limit(page_size))
        from app.services.fallback import can_fallback
        rows = []
        for task in items:
            transfer = session.get(Transfer, task.id)
            rows.append(serialize(task) | {
                "can_fallback": can_fallback(task),
                "transfer": serialize(transfer) if transfer else None,
            })
        return {"items": rows, "total": total, "page": page}


@secured.get("/api/v1/tasks/{task_id}")
def task_detail(request: Request, task_id: str, summary: bool = False,
                before_event: int = 0, event_limit: int = 50, files: bool = True,
                file_offset: int = 0):
    with request.app.state.db.session() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, detail="TASK_NOT_FOUND")
        result = serialize(task)
        from app.services.fallback import can_fallback
        result["can_fallback"] = can_fallback(task)
        removed = set(task.output.get("cleanup_deleted", []))
        result["output"] = dict(result["output"]) | {"cleanup_deleted_count": len(removed)}
        if summary or not files:
            result["output"] = {key: value for key, value in result["output"].items()
                                if key not in {"expected_manifest", "actual_manifest", "cleanup_deleted", "cleanup_manifest"}}
        else:
            offset = max(0, file_offset)
            result["output"].pop("cleanup_manifest", None)
            counts = {}
            for key in ("expected_manifest", "actual_manifest", "cleanup_deleted"):
                values = task.output.get(key, [])
                counts[key] = len(values)
                if key in task.output:
                    result["output"][key] = values[offset:offset + 200]
                    if key == "actual_manifest":
                        result["output"][key] = [
                            entry | {"cleanup_removed": entry["path"] in removed}
                            for entry in result["output"][key]
                        ]
            result["file_counts"] = counts
            result["files_more"] = any(count > offset + 200 for count in counts.values())
        transfer = session.get(Transfer, task_id)
        result["transfer"] = serialize(transfer) if transfer else None
        if summary:
            return result
        query = select(Event).where(Event.task_id == task_id)
        if before_event:
            query = query.where(Event.id < before_event)
        limit = min(100, max(1, event_limit))
        events = list(session.scalars(query.order_by(Event.id.desc()).limit(limit + 1)))
        steps = session.scalars(select(Step).where(Step.task_id == task_id)
                                .order_by(Step.updated_at.desc()).limit(100))
        return result | {"events": [serialize(x) for x in reversed(events[:limit])],
                         "events_more": len(events) > limit,
                         "steps": [serialize(x) for x in steps]}


def task_command(request, task_id, action, expected_version=None):
    with request.app.state.db.session() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, detail="TASK_NOT_FOUND")
        if expected_version is not None and task.version != expected_version:
            raise HTTPException(409, detail="TASK_VERSION_CONFLICT")
        if action == "hide":
            task.hidden = True
        elif action == "cancel":
            if task.state in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
                raise HTTPException(409, detail="TASK_ALREADY_TERMINAL")
            task.cancel_requested = True
            if task.source == "115" and not task.remote_id and task.stage in {
                "cloud_prepare", "cloud_inspect", "cloud_password",
            }:
                # Wake queued observation promptly without disturbing an in-flight request.
                for job in session.scalars(select(Job).where(
                    Job.kind == "cloud_extract", Job.state == "READY",
                    Job.payload["task_id"].as_string() == task.id,
                )):
                    job.due = time.time()
            if task.remote_id or task.stage in {"cloud_inspect", "cloud_export_unknown"}:
                record(session, task.id, "CANCEL_REQUESTED", task.stage,
                       "Remote operation may continue")
            elif task.state != "RUNNING":
                transition(session, task, "CANCELED", task.stage)
        elif action == "retry":
            if task.state != "FAILED":
                raise HTTPException(409, detail="ONLY_FAILED_TASK_CAN_RETRY")
            from app.services.execution import begin_execution
            begin_execution(session, task)
            transition(session, task, "READY", task.stage, manual_retry=True)
            identity = session.get(Identity, task.content_key)
            if identity:
                session.delete(identity)
            task.cancel_requested = False
            task.completed_at = None
            transfer = session.get(Transfer, task.id)
            if transfer:
                transfer.attempts, transfer.error, transfer.state = 0, "", "READY"
            if task.stage == "local_extract":
                task.output = {}
            if task.source == "115" and task.stage.startswith("cloud"):
                task.stage = "cloud_prepare"
                task.remote_id = ""
                task.password_attempts = 0
                task.output = {}
                enqueue(session, "archive:" + task.id, "cloud_extract", {"task_id": task.id})
            elif task.stage == "document_copy":
                enqueue(session, "document-copy:" + task.id, "document_copy", {"task_id": task.id})
            else:
                enqueue(session, "local:" + task.id, "local_extract", {"task_id": task.id})
        elif action == "approve":
            if task.stage == "identity_conflict" or task.wait_reason == "IDENTITY":
                task.wait_reason = ""
                transition(session, task, "READY", "cloud_prepare" if task.source == "115" else "cache")
                enqueue(session, ("archive:" if task.source == "115" else "local:") + task.id,
                        "cloud_extract" if task.source == "115" else "local_extract",
                        {"task_id": task.id})
            # Unverified 115 download routes cannot be approved by inventing a local path.
            elif task.source == "115":
                if task.stage == "cloud_password" and request.app.state.db.settings()["passwords"]:
                    task.password_attempts = 0
                    transition(session, task, "READY", "cloud_prepare")
                    enqueue(session, "archive:" + task.id, "cloud_extract", {"task_id": task.id})
                else:
                    if task.output.get("failure_archive_result") in {"PENDING", "FAILED"}:
                        raise HTTPException(409, detail="FAILED_ARCHIVE_RECONCILIATION_REQUIRED")
                    failed_cloud = task.state == "FAILED" and task.stage in {
                        "cloud_prepare", "cloud_inspect", "cloud_output", "cloud_progress", "cloud_verify",
                    } and task.error in {"REMOTE_REJECTED", "CLOUD_CONTENT_FAILED", "CLOUD_EXPORT_REJECTED"}
                    if (task.stage != "local_fallback" and not failed_cloud) or not (
                        task.locator.get("pickcode")
                    ):
                        raise HTTPException(409, detail="FALLBACK_CAPABILITY_REQUIRED")
                    if failed_cloud:
                        if session.scalar(select(Job.id).where(
                            Job.payload["task_id"].as_string() == task.id,
                            Job.state.in_(["RUNNING", "READY", "RECONCILING"]),
                        )):
                            raise HTTPException(409, detail="TASK_OPERATIONS_PENDING")
                        from app.services.execution import begin_execution
                        begin_execution(session, task)
                        task.output, task.remote_id, task.completed_at = {}, "", None
                        task.cancel_requested = False
                        identity = session.get(Identity, task.content_key)
                        if identity:
                            session.delete(identity)
                    transition(session, task, "READY", "cache", manual_retry=failed_cloud)
                    task.policy = task.policy | {"fallback_approved": True}
                    enqueue(session, "local:" + task.id, "local_extract", {"task_id": task.id})
            else:
                from app.adapters.extract_worker import extractor_available
                if not extractor_available(request.app.state.config):
                    raise HTTPException(409, detail="EXTRACTOR_CAPABILITY_REQUIRED")
                transition(session, task, "READY", "local_extract")
                enqueue(session, "local:" + task.id, "local_extract", {"task_id": task.id})
        record(session, task.id, "USER_COMMAND", task.stage, action)
        from app.api.management import audit
        audit(request, session, "TASK_" + action.upper(), task.id)
    request.app.state.engine.wake.set()
    return {"success": True, "operation_id": task_id}


@secured.post("/api/v1/tasks/{task_id}/commands", status_code=202)
def command(request: Request, task_id: str, body: TaskCommand):
    return task_command(request, task_id, body.action, body.expected_version)


@secured.get("/api/115/offline")
@secured.get("/api/v1/offline")
def list_offline(request: Request, page: int = 1, page_size: int = 30):
    db = request.app.state.db
    with db.session() as session:
        batches = session.scalars(select(Batch).where(~Batch.hidden).order_by(
            Batch.created_at.desc()).offset(max(0, page - 1) * min(100, page_size))
            .limit(max(1, min(100, page_size))))
        result = []
        for batch in batches:
            items = list(session.scalars(select(Offline).join(
                BatchItem, BatchItem.task_id == Offline.id,
            ).where(BatchItem.batch_id == batch.id)))
            task_rows = []
            for item in items:
                linked = list(session.scalars(select(Task).join(
                    ResourceLink, ResourceLink.task_id == Task.id,
                ).where(ResourceLink.offline_id == item.id)))
                from app.services.archives import is_archive
                archives = [task for task in linked if is_archive(task.name)]
                active = [task for task in archives if task.state not in {
                    "SUCCEEDED", "FAILED", "CANCELED", "SKIPPED",
                }]
                failures = [task for task in archives if task.state == "FAILED"]
                processing = (active or failures or archives or linked)
                pipeline_stage = processing[0].stage if processing else item.stage
                row = serialize(item)
                row.pop("original_link")
                task_rows.append(row | {"kind": item.kind, "info_hash": item.hash_value,
                                        "pipeline_status": pipeline_stage,
                                        "artifact_count": len(linked),
                                        "archives": [serialize(x) for x in linked]})
            result.append(serialize(batch) | {"target_name": batch.target_date, "tasks": task_rows,
                                             "check_count": max([x.check_count for x in items], default=0),
                                             "next_check": min([x.next_check for x in items
                                                                if x.next_check], default=None)})
        return {"batches": result}


@secured.get("/api/v1/offline/{task_id}")
def offline_detail(request: Request, task_id: str):
    with request.app.state.db.session() as session:
        task = session.get(Offline, task_id)
        if not task:
            raise HTTPException(404, detail="OFFLINE_TASK_NOT_FOUND")
        return serialize(task) | {"executions": [serialize(x) for x in session.scalars(
            select(OfflineExecution).where(OfflineExecution.offline_id == task_id).order_by(OfflineExecution.generation))]}


@secured.post("/api/115/offline/import", status_code=202)
@secured.post("/api/v1/offline/import", status_code=202)
async def import_offline(request: Request):
    if (request.app.state.config.adapter == "p115"
            and not request.app.state.db.settings().get("cloud_root_cid")):
        raise HTTPException(409, detail="CLOUD_ROOT_REQUIRED")
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = ImportRequest.model_validate(await request.json())
        text, auto = body.text, body.auto_extract
    elif content_type.startswith("multipart/form-data") or content_type.startswith(
        "application/x-www-form-urlencoded"
    ):
        form = await request.form(max_files=1, max_fields=5)
        text = str(form.get("text", ""))
        auto = str(form.get("auto_extract", "true")).lower() == "true"
        upload = form.get("file")
        if upload:
            raw = await upload.read(request.app.state.config.max_document_bytes + 1)
            if len(raw) > request.app.state.config.max_document_bytes:
                raise HTTPException(413, detail="UPLOAD_SIZE_LIMIT")
            text += "\n" + raw.decode("utf-8-sig")
    else:
        text = (await request.body()).decode("utf-8-sig")
        auto = True
    if len(text.encode()) > request.app.state.config.max_document_bytes:
        raise HTTPException(413, detail="IMPORT_SIZE_LIMIT")
    engine = request.app.state.engine
    idempotency = request.headers.get("idempotency-key", "")
    if len(idempotency) > 128:
        raise HTTPException(422, detail="IDEMPOTENCY_KEY_TOO_LONG")
    fingerprint = hashlib.sha256(json.dumps([text, auto]).encode()).hexdigest()
    async with engine.offline.import_lock:
        with request.app.state.db.session() as session:
            owner = getattr(request.state, "token_key", "admin")
            key = "idempotency:" + hashlib.sha256((owner + ":" + idempotency).encode()).hexdigest()
            previous = session.get(Settings, key) if idempotency else None
            if previous:
                if previous.value["fingerprint"] != fingerprint:
                    raise HTTPException(409, detail="IDEMPOTENCY_KEY_CONFLICT")
                return previous.value["result"]
            result = engine.offline.register(session, text, auto)
            if idempotency:
                session.add(Settings(key=key, value={"fingerprint": fingerprint, "result": result}))
            if getattr(request.state, "token_key", ""):
                session.add(Settings(key="batch-owner:" + result["batch_id"],
                                     value={"token": request.state.token_key}))
        engine.wake.set()
    return result | {"operation_id": result["batch_id"]}


@secured.post("/api/115/offline/refresh", status_code=202)
async def refresh_offline(request: Request):
    engine = request.app.state.engine
    with request.app.state.db.session() as session:
        engine.offline.schedule_poll(session, time.time(), manual=True)
    engine.wake.set()
    return {"success": True, "operation_id": "offline-poll:115-main"}


@secured.post("/api/115/offline/retry", status_code=202)
async def retry_offline(request: Request):
    body = await request.json()
    count = request.app.state.engine.offline.retry(body["task_id"])
    request.app.state.engine.wake.set()
    return {"success": True, "retried": count}


@secured.post("/api/115/offline/retry-all", status_code=202)
def retry_all(request: Request):
    count = request.app.state.engine.offline.retry()
    request.app.state.engine.wake.set()
    return {"success": True, "retried": count}


@secured.post("/api/115/sync", status_code=202)
@secured.post("/api/115/offline/scan-txt", status_code=202)
@secured.post("/api/v1/system/refresh", status_code=202)
def refresh_sources(request: Request):
    with request.app.state.db.session() as session:
        for source in session.scalars(select(Source).where(Source.enabled)):
            kind = "cloud_scan" if source.kind == "115" else "local_scan"
            payload = {"cid": source.config["cid"], "offline_only": False} if kind == "cloud_scan" else {
                "source_id": source.id,
            }
            if kind == "cloud_scan":
                payload.update({"source_id": source.id, "one_shot": True, "reason": "手动刷新监测来源"})
            key = "scan-cid:" + payload["cid"] if kind == "cloud_scan" else "scan-source:" + source.id
            enqueue(session, key, kind, payload)
        for cid in set(session.scalars(select(Offline.target_cid).where(
            Offline.target_cid != "", Offline.status.not_in(["failed", "submit_failed", "canceled"]),
        ))):
            enqueue(session, "scan-cid:" + cid, "cloud_scan", {
                "cid": cid, "offline_only": True, "one_shot": True, "reason": "手动刷新离线目录",
            })
    request.app.state.engine.wake.set()
    return {"success": True, "message": "已加入来源扫描队列"}


@secured.get("/api/v1/settings")
def get_settings(request: Request):
    settings = request.app.state.db.settings()
    return {k: settings[k] for k in DEFAULT_SETTINGS if k != "passwords"} | {
        "password_count": len(settings["passwords"]),
        "notification_api_key_configured": bool(settings.get("notification_api_key")),
        "paths": {"data": str(request.app.state.config.data_dir)},
    }


@secured.post("/api/settings")
@secured.post("/api/v1/settings")
async def set_settings(request: Request):
    body = await request.json()
    allowed = set(DEFAULT_SETTINGS) - {"passwords", "paused", "mappings"}
    allowed -= {"cloud_root_cid", "cloud_archive_cid", "cloud_failed_cid", "life_monitor_started"}
    if set(body) - allowed:
        raise HTTPException(422, detail="UNKNOWN_OR_SECRET_SETTINGS")
    for key, value in body.items():
        if key == "local_extract_concurrency" and (type(value) is not int or not 1 <= value <= 4):
            raise HTTPException(422, detail="INVALID_LOCAL_CONCURRENCY")
        if key.endswith("_seconds") and (type(value) is not int or value < 15 or value > 86400):
            raise HTTPException(422, detail="INVALID_INTERVAL")
        if key.endswith("_cid") and (not isinstance(value, str) or (
            not value.isdigit() and not (key == "offline_extract_cid" and value == "")
        )):
            raise HTTPException(422, detail="INVALID_CID")
        if key == "cloud_extract_by_date" and type(value) is not bool:
            raise HTTPException(422, detail="INVALID_DATE_SETTING")
        if key == "life_monitor_enabled" and type(value) is not bool:
            raise HTTPException(422, detail="INVALID_MONITOR_SETTING")
        if key.startswith("notification_") and key not in {
            "notification_webhook", "notification_provider", "notification_template",
        } and type(value) is not bool:
            raise HTTPException(422, detail="INVALID_NOTIFICATION_SETTING")
        if key == "notification_provider" and value not in {"ms", "webhook"}:
            raise HTTPException(422, detail="INVALID_NOTIFICATION_PROVIDER")
        if key in {"notification_webhook", "notification_template"} and (
            not isinstance(value, str) or len(value) > 8192
        ):
            raise HTTPException(422, detail="INVALID_NOTIFICATION_SETTING")
    webhook = body.get("notification_webhook", "")
    if webhook:
        parsed = urlsplit(webhook)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username or parsed.password or parsed.fragment
            or not parsed.hostname
            or ((request.app.state.config.allowed_webhook_hosts or not request.app.state.config.local_no_auth)
                and parsed.hostname not in request.app.state.config.allowed_webhook_hosts)
        ):
            raise HTTPException(422, detail="WEBHOOK_HOST_REQUIRES_EXPLICIT_ALLOWLIST")
    if body.get("life_monitor_enabled") and not request.app.state.db.settings()["life_monitor_enabled"]:
        body["life_monitor_started"] = time.time()
    request.app.state.db.set_settings(body)
    request.app.state.engine.wake.set()
    return {"success": True}


@secured.get("/api/v1/passwords")
def list_passwords(request: Request):
    if getattr(request.state, "role", "admin") != "admin":
        raise HTTPException(403, detail="ADMIN_CONFIGURATION_PERMISSION")
    return Response(
        content=json.dumps({"items": [request.app.state.secrets.decrypt(value)
                                    for value in request.app.state.db.settings()["passwords"]]}),
        media_type="application/json", headers={"Cache-Control": "no-store"},
    )


@secured.post("/api/passwords")
async def passwords(request: Request):
    body = await request.json()
    settings = request.app.state.db.settings()
    values = list(settings["passwords"])
    if body.get("action") == "add":
        password = body.get("password", "")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024 or len(values) >= 100:
            raise HTTPException(422, detail="PASSWORD_LIMIT")
        values.append(request.app.state.secrets.encrypt(password))
    elif body.get("action") == "remove":
        index = body.get("index")
        if not isinstance(index, int) or not 0 <= index < len(values):
            raise HTTPException(422, detail="INVALID_PASSWORD_INDEX")
        values.pop(index)
    else:
        raise HTTPException(422, detail="INVALID_PASSWORD_ACTION")
    request.app.state.db.set_settings({"passwords": values})
    return {"success": True, "count": len(values)}


@secured.get("/api/v1/sources")
def sources(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [serialize(x) for x in session.scalars(select(Source))]}


def validate_source(request, body, source_id=""):
    config = request.app.state.config
    allowed_fields = {
        "115": {"cid", "extract_cid"}, "local": {"path", "output_path", "exclude_paths"},
        "inbox": {"path", "exclude_paths"},
    }
    if set(body.config) - allowed_fields[body.kind]:
        raise HTTPException(422, detail="UNKNOWN_SOURCE_CONFIG_FIELD")
    if "output_path" in body.config and not isinstance(body.config["output_path"], str):
        raise HTTPException(422, detail="INVALID_OUTPUT_PATH")
    if body.kind == "115":
        if not isinstance(body.config.get("cid"), str) or not body.config["cid"].isdigit():
            raise HTTPException(422, detail="INVALID_CID")
        if body.config["cid"] in {request.app.state.db.settings().get(key) for key in
                                  ("cloud_root_cid", "cloud_output_cid", "cloud_archive_cid", "cloud_failed_cid")
                                  if request.app.state.db.settings().get(key)}:
            raise HTTPException(422, detail="SOURCE_OUTPUT_LOOP")
        body.interval = 0
        if "extract_cid" in body.config and (
            not isinstance(body.config["extract_cid"], str) or
            (body.config["extract_cid"] and not body.config["extract_cid"].isdigit())
        ):
            raise HTTPException(422, detail="INVALID_EXTRACT_CID")
        if body.interval and body.interval < 1800:
            raise HTTPException(422, detail="CLOUD_SCAN_MINIMUM_1800_SECONDS")
        with request.app.state.db.session() as session:
            if session.scalar(select(Source).where(
                Source.kind == "115", Source.id != source_id,
                Source.config["cid"].as_string() == body.config["cid"],
            )):
                raise HTTPException(422, detail="DUPLICATE_CLOUD_SOURCE")
    else:
        path = Path(body.config.get("path", "")).resolve()
        allowed_roots = [config.local_path("watch"), config.local_path("inbox/links")] + (
            config.allowed_source_roots
        )
        if not any(path.is_relative_to(root.resolve()) for root in allowed_roots) or not path.is_dir():
            raise HTTPException(422, detail="SOURCE_NOT_IN_AUTHORIZED_ROOT")
        if config.local_root is not None:
            protected = [config.local_path(x) for x in (
                "output", "cache", "archive", "inbox/processed", "inbox/failed", "inbox/partial",
            )]
            if any(path.is_relative_to(x) or (
                x.is_relative_to(path) and not (
                    body.kind == "inbox" and path == config.local_path("inbox/links")
                    and x.is_relative_to(path)
                )
            ) for x in protected):
                raise HTTPException(422, detail="SOURCE_OUTPUT_LOOP")
        protected = [config.data_dir / x for x in (
            "config", "output", "cache", "archive", "inbox/processed", "inbox/failed",
        )]
        if any(path.is_relative_to(x) or x.is_relative_to(path) for x in protected):
            raise HTTPException(422, detail="SOURCE_OUTPUT_LOOP")
        with request.app.state.db.session() as session:
            for source in session.scalars(select(Source).where(Source.kind.in_(["local", "inbox"]))):
                other = Path(source.config["path"])
                if source.config.get("output_path"):
                    other_output = Path(source.config["output_path"]).resolve()
                    if path.is_relative_to(other_output) or other_output.is_relative_to(path):
                        raise HTTPException(422, detail="SOURCE_OUTPUT_LOOP")
                if (body.kind == "inbox") != (source.kind == "inbox") and (
                    path.is_relative_to(other) or other.is_relative_to(path)
                ):
                    raise HTTPException(422, detail="INBOX_MUST_BE_SEPARATE")
        body.config["path"] = str(path)
        excludes = body.config.get("exclude_paths", [])
        if not isinstance(excludes, list) or len(excludes) > 100 or any(
            not isinstance(value, str) or not 1 <= len(value) <= 1024 for value in excludes
        ):
            raise HTTPException(422, detail="INVALID_EXCLUDED_PATHS")
        normalized = []
        for exclude in excludes:
            candidate = Path(exclude).expanduser()
            candidate = (candidate if candidate.is_absolute() else path / candidate).resolve()
            if not candidate.is_relative_to(path):
                raise HTTPException(422, detail="EXCLUSION_OUTSIDE_SOURCE")
            normalized.append(str(candidate))
        if "exclude_paths" in body.config:
            body.config["exclude_paths"] = list(dict.fromkeys(normalized))
        if body.config.get("output_path"):
            output = Path(body.config["output_path"]).resolve()
            roots = [config.local_path("output")] + config.allowed_output_roots
            if not any(output.is_relative_to(root.resolve()) for root in roots):
                raise HTTPException(422, detail="OUTPUT_NOT_IN_AUTHORIZED_ROOT")
            if output.is_relative_to(path) or path.is_relative_to(output):
                raise HTTPException(422, detail="SOURCE_OUTPUT_LOOP")
            with request.app.state.db.session() as session:
                for other in session.scalars(select(Source).where(Source.kind.in_(
                    ["local", "inbox"],
                ))):
                    other_path = Path(other.config["path"]).resolve()
                    if output.is_relative_to(other_path) or other_path.is_relative_to(output):
                        raise HTTPException(422, detail="SOURCE_OUTPUT_LOOP")
            body.config["output_path"] = str(output)


@secured.post("/api/v1/sources", status_code=201)
def add_source(request: Request, body: SourceRequest):
    validate_source(request, body)
    with request.app.state.db.session() as session:
        source = Source(**body.model_dump())
        session.add(source)
        session.flush()
        result = serialize(source)
    request.app.state.engine.wake.set()
    return result


@secured.patch("/api/v1/sources/{source_id}")
async def toggle_source(request: Request, source_id: str):
    body = await request.json()
    if set(body) == {"enabled"} and type(body["enabled"]) is bool:
        with request.app.state.db.session() as session:
            source = session.get(Source, source_id)
            if not source:
                raise HTTPException(404, detail="SOURCE_NOT_FOUND")
            source.enabled = body["enabled"]
        return {"success": True}
    with request.app.state.db.session() as session:
        source = session.get(Source, source_id)
        if not source:
            raise HTTPException(404, detail="SOURCE_NOT_FOUND")
        if body.get("expected_revision") != source.revision:
            raise HTTPException(409, detail="SOURCE_REVISION_CONFLICT")
    body.pop("expected_revision", None)
    update = SourceRequest.model_validate(body)
    validate_source(request, update, source_id)
    with request.app.state.db.session() as session:
        source = session.get(Source, source_id)
        for key, value in update.model_dump().items():
            setattr(source, key, value)
        source.revision += 1
    return {"success": True}


@secured.delete("/api/v1/sources/{source_id}")
async def remove_source(request: Request, source_id: str):
    with request.app.state.db.session() as session:
        source = session.get(Source, source_id)
        if not source:
            raise HTTPException(404, detail="SOURCE_NOT_FOUND")
        key = ("scan-cid:" + source.config["cid"] if source.kind == "115"
               else "scan-source:" + source.id)
        scans = list(session.scalars(select(Job).where(Job.kind.in_(
            ["cloud_scan", "local_scan", "document"],
        ))))
        owned = [job for job in scans if job.key == key or job.payload.get("source_id") == source.id]
        if any(job.state in {"RUNNING", "LEASED", "RECONCILING"} for job in owned):
            raise HTTPException(409, detail="SOURCE_SCAN_BUSY")
        for job in owned:
            # Shared offline discovery must survive removal of the monitoring source.
            if source.kind == "115" and session.scalar(select(Offline.id).where(
                Offline.target_cid == source.config["cid"],
                Offline.status.not_in(["failed", "submit_failed", "canceled"]),
            )):
                payload = dict(job.payload)
                payload.pop("source_id", None)
                job.payload = payload | {"offline_only": True}
            else:
                session.delete(job)
        session.delete(source)
    watcher = request.app.state.engine.watchers.pop(source_id, None)
    if watcher:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
    request.app.state.engine.wake.set()
    return {"success": True}


@secured.get("/api/v1/jobs")
def jobs(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [serialize(x) for x in session.scalars(
            select(Job).order_by(Job.due.desc()).limit(100))]}


@secured.get("/api/v1/documents")
def documents(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [serialize(x) for x in session.scalars(select(DocumentImport).limit(100))]}


@secured.get("/api/v1/notifications")
def notifications(request: Request):
    with request.app.state.db.session() as session:
        return {"items": [
            serialize(outbox) | {"event": serialize(event)}
            for outbox, event in session.execute(
                select(Outbox, Event).join(Event, Outbox.event_id == Event.id)
                .order_by(Outbox.id.desc()).limit(100)
            )
        ]}


@secured.post("/api/notification")
async def notification_settings(request: Request):
    body = await request.json()
    allowed = {"notification_offline", "notification_cloud", "notification_local"}
    if set(body).issubset(allowed) and all(type(value) is bool for value in body.values()):
        request.app.state.db.set_settings(body)
        return {"success": True}
    if set(body) != {"api_key"} or not isinstance(body["api_key"], str) or len(body["api_key"]) > 4096:
        raise HTTPException(422, detail="INVALID_NOTIFICATION_SETTINGS")
    value = body["api_key"]
    request.app.state.db.set_settings({
        "notification_api_key": request.app.state.secrets.encrypt(value) if value else "",
    })
    return {"success": True}


@secured.post("/api/notification/test", status_code=202)
def test_notification(request: Request):
    with request.app.state.db.session() as session:
        record(session, "system", "TEST", "notification", "115work notification test", "local")
        enqueue(session, "outbox", "notifications", {})
    request.app.state.engine.wake.set()
    return {"success": True}


@secured.post("/api/notification/test-flow", status_code=202)
def test_notification_flow(request: Request):
    identifiers = []
    stages = [
        ("离线任务已提交", "offline"),
        ("离线任务完成", "offline"),
        ("发现压缩包", "cloud"),
        ("开始解压", "cloud"),
        ("原包处理完成", "cloud"),
        ("解压完成", "cloud"),
    ]
    with request.app.state.db.session() as session:
        for index, (title, category) in enumerate(stages, 1):
            event = record(session, "notification-flow-test", "TEST", "notification_flow_test",
                           f"【模拟测试 {index}/{len(stages)}】{title} · 测试压缩包.zip", category)
            identifiers.append(event.id)
        enqueue(session, "outbox", "notifications", {})
    request.app.state.engine.wake.set()
    return {"success": True, "event_ids": identifiers}


@secured.post("/api/tasks/system")
async def task_system(request: Request):
    action = (await request.json()).get("action")
    if action not in {"pause", "resume", "stop_clear"}:
        raise HTTPException(422, detail="INVALID_SYSTEM_ACTION")
    request.app.state.db.set_settings({"paused": action != "resume"})
    cleared = 0
    with request.app.state.db.session() as session:
        if action == "stop_clear":
            for task in session.scalars(select(Task).where(Task.state.in_(
                ["READY", "WAITING_RETRY", "WAITING_RESOURCE", "WAITING_APPROVAL"]),
                Task.remote_id == "", Task.stage.not_in(
                    ["cloud_inspect", "cloud_export_unknown", "cloud_output", "cloud_password"]),
            )):
                task.cancel_requested = True
                transition(session, task, "CANCELED", task.stage)
                cleared += 1
            for item in session.scalars(select(Offline).where(Offline.status == "queued")):
                item.status, item.stage, item.next_check = "canceled", "offline_submit", None
                record(session, item.id, "CANCELED", item.stage, "Unsubmitted execution canceled")
                cleared += 1
        record(session, "system", "CONTROL", "system", action)
    request.app.state.engine.wake.set()
    return {"success": True, "cleared": cleared}


@secured.post("/api/tasks/cancel", status_code=202)
async def cancel_task(request: Request):
    body = await request.json()
    return task_command(request, body.get("task_id") or body.get("key"), "cancel")


@secured.post("/api/history/delete")
async def hide_history(request: Request):
    body = await request.json()
    action = body.get("action", "delete")
    return task_command(request, body.get("task_id") or body.get("key"),
                        "hide" if action == "delete" else action)


@secured.post("/api/115/fallback", status_code=202)
async def fallback(request: Request):
    body = await request.json()
    return task_command(request, body.get("task_id") or body.get("key"), "approve")


@secured.post("/api/downloads/pause")
async def pause_downloads(request: Request):
    body = await request.json()
    paused = body.get("paused", True)
    if type(paused) is not bool:
        raise HTTPException(422, detail="INVALID_PAUSE_VALUE")
    request.app.state.db.set_settings({"downloads_paused": paused})
    return {"success": True}


@secured.post("/api/115/offline/clear")
async def clear_offline(request: Request):
    body = await request.json()
    db = request.app.state.db
    with db.session() as session:
        if body.get("action") == "reset":
            if not db.settings()["paused"]:
                raise HTTPException(409, detail="PAUSE_REQUIRED")
            if session.scalar(select(Offline).where(Offline.status.not_in(
                ["success", "failed", "submit_failed", "canceled"],
            )).limit(1)):
                raise HTTPException(409, detail="ACTIVE_OFFLINE_EXECUTIONS_EXIST")
            session.execute(delete(BatchItem))
            session.execute(delete(ResourceLink))
            session.execute(delete(OfflineExecution))
            session.execute(delete(Offline))
            session.execute(delete(Batch))
        else:
            query = select(Batch)
            if body.get("batch_id"):
                query = query.where(Batch.id == body["batch_id"])
            for batch in session.scalars(query):
                batch.hidden = True
        record(session, "system", "MAINTENANCE", "offline", "Local records changed; remote untouched")
    return {"success": True}


@secured.post("/api/maintenance")
@secured.post("/api/downloads/cleanup")
async def maintenance(request: Request):
    body = await request.json()
    action = body.get("action")
    with request.app.state.db.session() as session:
        if action in {"clear_history", "hide_history"}:
            for task in session.scalars(select(Task).where(Task.state.in_(
                ["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"],
            ))):
                task.hidden = True
        elif action in {"reset_history", "reset_identities"}:
            if not request.app.state.db.settings()["paused"]:
                raise HTTPException(409, detail="PAUSE_REQUIRED")
            terminal_ids = select(Task.id).where(Task.state.in_(
                ["SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"],
            ))
            session.execute(delete(Identity).where(Identity.task_id.in_(terminal_ids)))
        elif action == "clear_cache":
            enqueue(session, "cache-gc", "cache_gc", {})
        else:
            raise HTTPException(422, detail="UNKNOWN_MAINTENANCE_ACTION")
        record(session, "system", "MAINTENANCE", "system", action)
    return {"success": True}












@secured.get("/api/v1/events")
async def events(request: Request, after: int = 0, live: bool = False):
    try:
        after = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(422, detail="INVALID_EVENT_CURSOR") from None
    if live and not after and not request.headers.get("last-event-id"):
        with request.app.state.db.session() as session:
            after = session.scalar(select(func.max(Event.id))) or 0

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            with request.app.state.db.session() as session:
                rows = list(session.scalars(select(Event).where(Event.id > cursor)
                                           .order_by(Event.id).limit(100)))
            for event in rows:
                cursor = event.id
                yield f"id: {cursor}\ndata: {json.dumps(serialize(event))}\n\n"
            if not rows:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


router.include_router(secured)
