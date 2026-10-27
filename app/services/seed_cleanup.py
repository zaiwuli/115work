from pathlib import PurePosixPath

from app.persistence.models import Offline, Settings, Operation
from app.services.cloud_flatten import listing
from app.services.events import record


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - {"enabled", "mode", "extensions", "keywords", "scope", "minimum", "maximum", "empty_dirs", "dry_run", "auto_extract"}:
        raise ValueError("INVALID_SEED_CLEANUP")
    result = {"enabled": False, "mode": "exclude", "extensions": [], "keywords": [],
              "scope": "path", "minimum": None, "maximum": None, "empty_dirs": True,
              "dry_run": True, "auto_extract": False} | value
    for key in ("enabled", "empty_dirs", "dry_run", "auto_extract"):
        if type(result[key]) is not bool:
            raise ValueError("INVALID_SEED_CLEANUP")
    if result["mode"] not in {"all", "include", "exclude"} or result["scope"] not in {"path", "name"}:
        raise ValueError("INVALID_SEED_CLEANUP")
    for key in ("extensions", "keywords"):
        values = result[key]
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in values):
            raise ValueError("INVALID_SEED_CLEANUP")
        result[key] = list(dict.fromkeys(x.strip().casefold() for x in values))
    result["extensions"] = [x if x.startswith(".") else "." + x for x in result["extensions"]]
    if result["mode"] == "include" and not result["extensions"]:
        raise ValueError("SEED_KEEP_TYPES_REQUIRED")
    for key in ("minimum", "maximum"):
        if result[key] is not None and (type(result[key]) is not int or not 0 <= result[key] <= 2**63 - 1):
            raise ValueError("INVALID_SEED_CLEANUP_SIZE")
    if result["minimum"] is not None and result["maximum"] is not None and result["minimum"] > result["maximum"]:
        raise ValueError("INVALID_SEED_CLEANUP_SIZE")
    return result


def should_remove(path, size, policy):
    suffix = PurePosixPath(path).suffix.casefold()
    typed = suffix in policy["extensions"]
    return ((policy["mode"] == "include" and not typed)
            or (policy["mode"] == "exclude" and typed)
            or any(word in (path if policy["scope"] == "path" else PurePosixPath(path).name).casefold()
                   for word in policy["keywords"])
            or (policy["minimum"] is not None and size < policy["minimum"])
            or (policy["maximum"] is not None and size > policy["maximum"]))


class SeedCleanup:
    def __init__(self, db, store, adapter):
        self.db, self.store, self.adapter = db, store, adapter

    async def run(self, job):
        identifier = job.payload["offline_id"]
        with self.db.session() as session:
            self.store.owned(session, job)
            task = session.get(Offline, identifier)
            saved = session.get(Settings, "seed-policy:" + identifier)
            if not task or not saved or task.status != "success":
                return None
            if saved.value.get("result") in {"SUCCEEDED", "PREVIEW"}:
                return None
            policy = validate_policy(saved.value["policy"].get("seed_cleanup", {}))
        try:
            roots = [f for f in await listing(self.adapter, task.target_cid)
                     if f.is_dir and f.name == task.name]
            if len(roots) != 1:
                raise ValueError("SEED_RESOURCE_DIRECTORY_NOT_UNIQUE")
            root = roots[0]
            if not root.fid or root.fid in {"0", task.target_cid, task.root_cid}:
                raise ValueError("UNSAFE_SEED_ROOT")
            files, directories, queue, seen = [], [], [(root.fid, "")], set()
            while queue:
                cid, prefix = queue.pop(0)
                if cid in seen or len(seen) >= 1000:
                    raise ValueError("SEED_DIRECTORY_BUDGET")
                seen.add(cid)
                for file in await listing(self.adapter, cid):
                    if file.name in {"", ".", ".."} or "/" in file.name or "\\" in file.name:
                        raise ValueError("UNSAFE_SEED_PATH")
                    path = str(PurePosixPath(prefix) / file.name)
                    if file.is_dir:
                        if file.fid in seen or file.fid in {task.target_cid, task.root_cid, "0"}:
                            raise ValueError("UNSAFE_SEED_DIRECTORY")
                        directories.append((file, path))
                        queue.append((file.fid, path))
                    else:
                        files.append((file, path))
                    if len(files) + len(directories) > 10000:
                        raise ValueError("SEED_FILE_BUDGET")
            selected = [(f, p) for f, p in files if policy["enabled"] and should_remove(p, f.size, policy)]
            with self.db.session() as session:
                self.store.owned(session, job)
                saved = session.get(Settings, "seed-policy:" + identifier)
                saved.value = saved.value | {"matched": len(selected), "preview": [p for _, p in selected], "root_cid": root.fid}
            for file, path in selected if not policy["dry_run"] else []:
                await self.remove(job, identifier, file, path)
            if policy["enabled"] and policy["empty_dirs"] and not policy["dry_run"]:
                for directory, path in sorted(directories, key=lambda x: x[1].count("/"), reverse=True):
                    if not await listing(self.adapter, directory.fid):
                        await self.remove(job, identifier, directory, path, directory=True)
            if policy["auto_extract"] and not policy["dry_run"]:
                from app.services.archives import ArchiveService, is_archive
                from app.persistence.models import ResourceLink
                archives = ArchiveService(self.db, self.adapter, None, self.store, None)
                deleted_ids = {f.fid for f, _ in selected}
                with self.db.session() as session:
                    self.store.owned(session, job)
                    for file, _ in files:
                        if file.fid in deleted_ids or not is_archive(file.name):
                            continue
                        archive = archives.adopt(session, file, True, offline_id=identifier)
                        if archive:
                            archive.policy = archive.policy | {
                                "seed_in_place": True, "cloud_output_cid": file.cid,
                                "output_date": False, "squash_root": False,
                                "original_action": "retain", "auto_fallback": False,
                                "cloud_failed_cid": "", "cleanup_enabled": False,
                            }
                            session.merge(ResourceLink(offline_id=identifier, task_id=archive.id,
                                                       evidence="seed-in-place"))
            with self.db.session() as session:
                self.store.owned(session, job)
                saved = session.get(Settings, "seed-policy:" + identifier)
                saved.value = saved.value | {"result": "PREVIEW" if policy["enabled"] and policy["dry_run"] else "SUCCEEDED", "error": ""}
                task = session.get(Offline, identifier)
                task.stage, task.error = "complete", ""
                record(session, identifier, "SEED_CLEANUP_COMPLETED", "complete",
                       f"种子下载成功；清理匹配 {len(selected)} 项；" + ("仅预演" if policy["dry_run"] else "清理完成"))
        except Exception as exc:
            with self.db.session() as session:
                self.store.owned(session, job)
                saved = session.get(Settings, "seed-policy:" + identifier)
                if saved:
                    saved.value = saved.value | {"result": "FAILED", "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
                    task = session.get(Offline, identifier)
                    task.stage, task.error = "seed_cleanup", "SEED_CLEANUP_FAILED"
                    record(session, identifier, "SEED_CLEANUP_FAILED", "seed_cleanup", "下载成功，清理失败：" + saved.value["error"])
            raise

    async def remove(self, job, identifier, file, path, directory=False):
        key = "seed-delete:" + identifier + ":" + file.fid
        with self.db.session() as session:
            self.store.owned(session, job)
            operation = session.get(Operation, key)
            if operation:
                if operation.status == "COMPLETED":
                    return
                raise ValueError("SEED_DELETE_REQUIRES_RECONCILIATION")
            session.add(Operation(key=key, task_id=identifier, kind="seed_cleanup",
                                  evidence={"fid": file.fid, "cid": file.cid, "path": path}))
        if directory and await listing(self.adapter, file.fid):
            raise ValueError("SEED_DIRECTORY_NOT_EMPTY")
        await self.adapter.delete_verified({"fid": file.fid, "cid": file.cid,
                                            "size": file.size, "sha1": file.sha1})
        with self.db.session() as session:
            self.store.owned(session, job)
            session.get(Operation, key).status = "COMPLETED"
            saved = session.get(Settings, "seed-policy:" + identifier)
            field = "deleted_directories" if directory else "deleted_files"
            saved.value = saved.value | {field: saved.value.get(field, []) + [path]}
            record(session, identifier, "SEED_FILE_DELETED", "seed_cleanup", "已清理：" + path)
