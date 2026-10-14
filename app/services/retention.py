import asyncio
import hashlib
import json
import shutil
import time
from pathlib import Path

from sqlalchemy import select

from app.domain.paths import contained
from app.persistence.models import CacheEntry, Operation, Rule, Task
from app.services.events import record
from app.services.execution import operation_scope


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


class RetentionService:
    def __init__(self, db, config, store, adapter):
        self.db, self.config, self.store, self.adapter = db, config, store, adapter

    async def failed_archive(self, job):
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Task, job.payload["task_id"])
            if task.output.get("failure_archive_result") == "SUCCEEDED":
                return None
            if task.stage != "local_fallback" or task.source != "115" or task.cancel_requested:
                raise ValueError("CLOUD_CONTENT_FAILURE_REQUIRED")
            cid = task.policy.get("cloud_failed_cid")
            if not cid or cid == task.locator["cid"]:
                raise ValueError("FAILED_ARCHIVE_CID_REQUIRED")
            key = "failed-archive:" + operation_scope(task)
            operation = session.get(Operation, key)
            if operation:
                raise ValueError("FAILED_ARCHIVE_REQUIRES_RECONCILIATION")
            session.add(Operation(key=key, task_id=task.id, kind="failure_archive",
                                  evidence={"from_cid": task.locator["cid"], "to_cid": cid}))
        try:
            await self.adapter.move_verified(task.locator, cid)
        except Exception:
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(Task, task.id)
                current.output = current.output | {"failure_archive_result": "FAILED"}
                record(session, task.id, "FAILURE_ARCHIVE_FAILED", "failure_archive",
                       "Move result requires reconciliation")
            raise
        with self.db.session() as session:
            self.store.owned(session, job)
            current = session.get(Task, task.id)
            current.locator = current.locator | {"cid": cid, "original_cid": task.locator["cid"]}
            current.output = current.output | {"failure_archive_result": "SUCCEEDED"}
            session.get(Operation, key).status = "COMPLETED"
            record(session, task.id, "FAILURE_ARCHIVE_SUCCEEDED", "failure_archive",
                   "Original archive moved; local fallback remains independent")
            from app.services.fallback import approve_automatic
            approve_automatic(session, current)
        return None

    async def run(self, job):
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Task, job.payload["task_id"])
            if task.state != "SUCCEEDED":
                raise ValueError("PROCESSING_SUCCESS_REQUIRED")
            action = task.policy.get("original_action", "retain")
            if (task.policy.get("cleanup_enabled") or task.policy.get("squash_root")) and task.policy.get("rule_id"):
                rule = session.get(Rule, task.policy["rule_id"])
                if not rule or not rule.enabled or rule.revision != task.policy["rule_revision"]:
                    raise ValueError("RETENTION_RULE_REVISION_CHANGED")
        from app.services.output_cleanup import OutputCleanup
        if task.source == "115" and not task.output.get("path") and task.policy.get("squash_root", True):
            from app.services.cloud_flatten import flatten
            await flatten(self.db, self.store, self.adapter, task, job)
            with self.db.session() as session:
                task = session.get(Task, task.id)
        await OutputCleanup(self.db, self.store, self.adapter).run(task, job)
        with self.db.session() as session:
            task = session.get(Task, task.id)
            if action == "retain":
                if (task.policy.get("cleanup_enabled") or task.output.get("flatten_result")) and task.output.get("postprocess_result") != "SUCCEEDED":
                    task.output = task.output | {"postprocess_result": "SUCCEEDED"}
                    record(session, task.id, "SUCCEEDED", "complete", "解压及产物清理完成",
                           "cloud" if task.source == "115" else "local")
                return None
            if task.policy.get("rule_id"):
                rule = session.get(Rule, task.policy["rule_id"])
                if not rule or not rule.enabled or rule.revision != task.policy["rule_revision"]:
                    raise ValueError("RETENTION_RULE_REVISION_CHANGED")
            if action == "delete" and not task.policy.get("delete_approved"):
                raise ValueError("DELETE_APPROVAL_REQUIRED")
            if action == "delete" and task.completed_at + self.config.delete_delay_seconds > time.time():
                return task.completed_at + self.config.delete_delay_seconds
        try:
            if task.source == "115":
                verification = task.output.get("verification")
                if verification == "sha256_manifest":
                    from app.services.local import LocalService
                    manifest = json.loads(Path(task.output["manifest"]).read_text(encoding="utf-8"))
                    from app.services.output_cleanup import remaining_manifest
                    manifest = remaining_manifest(manifest, task.output)
                    if not await asyncio.to_thread(LocalService.verify_manifest,
                                                   Path(task.output["path"]), manifest):
                        raise ValueError("OUTPUT_MANIFEST_MISMATCH")
                elif verification not in {"remote_manifest", "remote_reported_complete"}:
                    raise ValueError("STRONG_REMOTE_OUTPUT_VERIFICATION_REQUIRED")
                key = f"retain:{operation_scope(task)}:115:{action}"
                with self.db.session() as session:
                    self.store.owned(session, job)
                    operation = session.get(Operation, key)
                    if operation and operation.status == "COMPLETED":
                        return None
                    if operation:
                        raise ValueError("REMOTE_RETENTION_REQUIRES_RECONCILIATION")
                    session.add(Operation(key=key, task_id=task.id, kind=action,
                                          evidence={"locator": task.locator,
                                                    "destination": task.policy.get("archive_cid", "")}))
                if action == "archive":
                    cid = task.policy.get("archive_cid", "")
                    if not cid or cid == task.locator["cid"]:
                        raise ValueError("ARCHIVE_CID_REQUIRED")
                    await self.adapter.move_verified(task.locator, cid)
                else:
                    await self.adapter.delete_verified(task.locator)
                with self.db.session() as session:
                    self.store.owned(session, job)
                    session.get(Operation, key).status = "COMPLETED"
                    if action == "archive":
                        current = session.get(Task, task.id)
                        current.locator = current.locator | {"cid": cid,
                                                            "original_cid": task.locator["cid"]}
            else:
                from app.services.local import LocalService
                manifest = json.loads(Path(task.output["manifest"]).read_text(encoding="utf-8"))
                from app.services.output_cleanup import remaining_manifest
                manifest = remaining_manifest(manifest, task.output)
                if not await asyncio.to_thread(LocalService.verify_manifest,
                                               Path(task.output["path"]), manifest):
                    raise ValueError("OUTPUT_MANIFEST_MISMATCH")
                for part in task.locator.get("parts", [task.locator]):
                    source = contained(Path(task.locator["root"]), Path(part["path"]))
                    key = f"retain:{operation_scope(task)}:{source.name}:{action}"
                    with self.db.session() as session:
                        self.store.owned(session, job)
                        operation = session.get(Operation, key)
                        if operation and operation.status == "COMPLETED":
                            continue
                        destination = self.config.local_path("archive") / task.id / source.name
                        if operation is None:
                            session.add(Operation(key=key, task_id=task.id, kind=action,
                                                  evidence={"destination": str(destination)}))
                    if not source.exists():
                        if action == "delete":
                            raise ValueError("DELETE_RESULT_REQUIRES_RECONCILIATION")
                        if not destination.is_file():
                            raise ValueError("ARCHIVE_RESULT_REQUIRES_RECONCILIATION")
                    else:
                        info = source.stat()
                        if info.st_size != part.get("size", task.size) or (
                            info.st_mtime_ns != part["mtime_ns"]
                        ):
                            raise ValueError("SOURCE_VERSION_CHANGED")
                        if action == "archive":
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if destination.exists():
                                if await asyncio.to_thread(digest, source) != await asyncio.to_thread(
                                    digest, destination
                                ):
                                    raise ValueError("ARCHIVE_DESTINATION_CONFLICT")
                            else:
                                temporary = destination.with_name(destination.name + ".part")
                                await asyncio.to_thread(shutil.copy2, source, temporary)
                                if await asyncio.to_thread(digest, source) != await asyncio.to_thread(
                                    digest, temporary
                                ):
                                    raise ValueError("ARCHIVE_COPY_MISMATCH")
                                temporary.rename(destination)
                        with self.db.session() as session:
                            self.store.owned(session, job)
                            source.unlink()
                    with self.db.session() as session:
                        self.store.owned(session, job)
                        session.get(Operation, key).status = "COMPLETED"
            with self.db.session() as session:
                self.store.owned(session, job)
                current = session.get(Task, task.id)
                current.output = current.output | {"postprocess_result": "SUCCEEDED"}
                record(session, task.id, "POSTPROCESS_SUCCEEDED", "postprocess", action, "local")
                if task.policy.get("cleanup_enabled") or current.output.get("flatten_result"):
                    record(session, task.id, "SUCCEEDED", "complete", "解压、产物清理及原包后处理完成",
                           "cloud" if task.source == "115" else "local")
            return None
        except Exception as exc:
            with self.db.session() as session:
                current = session.get(Task, task.id)
                current.output = current.output | {"postprocess_result": "FAILED"}
                record(session, task.id, "POSTPROCESS_FAILED", "postprocess",
                       str(exc) if isinstance(exc, ValueError) else "POSTPROCESS_FAILED", "local")
            raise

    def cache_preview(self):
        with self.db.session() as session:
            entries = list(session.scalars(select(CacheEntry)))
            tasks = {task.id: task for task in session.scalars(select(Task))}
        return [{"task_id": entry.task_id, "size": entry.size, "path": entry.path,
                 "eligible": tasks[entry.task_id].state in {"SUCCEEDED", "CANCELED", "SKIPPED"},
                 "reason": "" if tasks[entry.task_id].state in {"SUCCEEDED", "CANCELED", "SKIPPED"}
                 else "TASK_REFERENCED"} for entry in entries]

    async def cleanup(self, job):
        for item in self.cache_preview():
            if not item["eligible"]:
                continue
            root = self.config.local_path("cache")
            path = contained(root, Path(item["path"]))
            if path.parent != root or path.name != item["task_id"]:
                raise ValueError("CACHE_ROOT_MISMATCH")
            with self.db.session() as session:
                self.store.owned(session, job)
                task = session.get(Task, item["task_id"])
                if task.state not in {"SUCCEEDED", "CANCELED", "SKIPPED"}:
                    continue
                if path.exists():
                    shutil.rmtree(path)
                session.delete(session.get(CacheEntry, task.id))
                record(session, task.id, "CACHE_CLEANED", "cache", "Unreferenced cache removed")
        return None
