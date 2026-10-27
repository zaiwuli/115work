import asyncio
import json
import re
from pathlib import Path, PurePosixPath

from app.domain.paths import contained
from app.persistence.models import Task
from app.services.events import record


def suffixes(values):
    if not isinstance(values, list) or len(values) > 50 or any(
        not isinstance(x, str) or not re.fullmatch(r"\.[a-zA-Z0-9_-]{1,24}", x) for x in values
    ):
        raise ValueError("INVALID_CLEANUP_SUFFIXES")
    return list(dict.fromkeys(x.lower() for x in values))


def remaining_manifest(manifest, output):
    removed = set(output.get("cleanup_deleted", []))
    files = [x for x in manifest.get("files", []) if x["path"] not in removed]
    return manifest | {"files": files, "cleanup_verified_empty": bool(manifest.get("files")) and not files}


class OutputCleanup:
    def __init__(self, db, store, adapter):
        self.db, self.store, self.adapter = db, store, adapter

    async def run(self, task, job):
        if not task.policy.get("cleanup_enabled") or task.output.get("cleanup_result") == "SUCCEEDED":
            return
        deleted = list(task.output.get("cleanup_deleted", []))
        matched = []
        try:
            extensions = suffixes(task.policy.get("cleanup_suffixes", []))
            if not extensions or task.state != "SUCCEEDED" or task.cancel_requested:
                raise ValueError("VERIFIED_OUTPUT_REQUIRED")
            local = bool(task.output.get("path"))
            if task.output.get("verification") not in {"sha256_manifest", "remote_manifest", "remote_reported_complete"}:
                raise ValueError("VERIFIED_OUTPUT_REQUIRED")
            directory_cache = {}
            if local:
                manifest = json.loads(Path(task.output["manifest"]).read_text(encoding="utf-8"))
                entries = manifest["files"]
            else:
                entries = task.output.get("cleanup_manifest")
                if entries is None:
                    from app.services.cloud_flatten import listing
                    queue, entries, visited = [(task.output["cid"], "")], [], set()
                    while queue:
                        cid, prefix = queue.pop(0)
                        if cid in visited or len(visited) >= 1000:
                            raise ValueError("OUTPUT_MANIFEST_BUDGET")
                        visited.add(cid)
                        page = await listing(self.adapter, cid)
                        directory_cache[cid] = page
                        for file in page:
                            path = str(PurePosixPath(prefix) / file.name)
                            if file.is_dir:
                                if task.policy.get("cleanup_recursive", True):
                                    queue.append((file.fid, path))
                            else:
                                entries.append({"path": path, "size": file.size,
                                                "fid": file.fid, "cid": cid, "sha1": file.sha1})
                            if len(entries) + len(queue) > 10000:
                                raise ValueError("OUTPUT_MANIFEST_BUDGET")
                    with self.db.session() as session:
                        self.store.owned(session, job)
                        current = session.get(Task, task.id)
                        current.output = current.output | {
                            "cleanup_manifest": entries, "actual_manifest": entries,
                            "manifest_source": "suffix_cleanup",
                        }
            matched = [x for x in entries if PurePosixPath(x["path"]).suffix.lower() in extensions
                       and (task.policy.get("cleanup_recursive", True) or "/" not in x["path"])]
            for entry in matched:
                relative = PurePosixPath(entry["path"])
                if relative.is_absolute() or ".." in relative.parts or "\\" in entry["path"]:
                    raise ValueError("UNSAFE_OUTPUT_PATH")
                if entry["path"] in deleted:
                    continue
                with self.db.session() as session:
                    self.store.owned(session, job)
                if local:
                    root = Path(task.output["path"])
                    target = root / entry["path"]
                    if any(part.is_symlink() for part in [target, *target.parents]):
                        raise ValueError("OUTPUT_SYMLINK_REJECTED")
                    target = contained(root, target)
                    if target.exists():
                        from app.services.retention import digest
                        if not target.is_file() or target.stat().st_size != entry["size"] or (
                            await asyncio.to_thread(digest, target) != entry["sha256"]
                        ):
                            raise ValueError("OUTPUT_VERSION_CHANGED")
                        target.unlink()
                else:
                    # Resolve each component within this task's output, never by a global file search.
                    cid = task.output["cid"]
                    file = None
                    for index, component in enumerate(relative.parts):
                        if cid not in directory_cache:
                            from app.services.cloud_flatten import listing
                            directory_cache[cid] = await listing(self.adapter, cid)
                        found = [x for x in directory_cache[cid] if x.name == component]
                        if not found:
                            file = None
                            break
                        if len(found) != 1:
                            raise ValueError("AMBIGUOUS_OUTPUT_FILE")
                        file = found[0]
                        if index < len(relative.parts) - 1:
                            if not file.is_dir:
                                raise ValueError("OUTPUT_PATH_CHANGED")
                            cid = file.fid
                    if file is not None:
                        if file.is_dir or (entry.get("size") is not None and file.size != entry["size"]):
                            raise ValueError("OUTPUT_VERSION_CHANGED")
                        if any(entry.get(key) and entry[key] != value for key, value in (
                            ("fid", file.fid), ("cid", cid), ("sha1", file.sha1),
                        )):
                            raise ValueError("OUTPUT_VERSION_CHANGED")
                        await self.adapter.delete_verified({
                            "fid": file.fid, "cid": cid, "size": file.size, "sha1": file.sha1,
                        })
                deleted.append(entry["path"])
                with self.db.session() as session:
                    self.store.owned(session, job)
                    current = session.get(Task, task.id)
                    current.output = current.output | {"cleanup_deleted": deleted[:]}
                    record(session, task.id, "OUTPUT_FILE_DELETED", "output_cleanup",
                           "已清理解压产物：" + entry["path"])
            with self.db.session() as session:
                current = session.get(Task, task.id)
                current.output = current.output | {
                    "cleanup_result": "SUCCEEDED", "cleanup_matched": len(matched),
                    "cleanup_failed": 0, "cleanup_error": "",
                }
                record(session, task.id, "OUTPUT_CLEANUP_SUCCEEDED", "output_cleanup",
                       f"后缀清理完成：匹配 {len(matched)}，已删除 {len(deleted)}", "cloud" if task.source == "115" and not task.policy.get("fallback_approved") else "local")
        except Exception as exc:
            with self.db.session() as session:
                current = session.get(Task, task.id)
                current.output = current.output | {
                    "cleanup_result": "FAILED", "cleanup_matched": len(matched),
                    "cleanup_failed": max(0, len(matched) - len(deleted)),
                    "cleanup_error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__,
                    "postprocess_result": "FAILED",
                }
                record(session, task.id, "OUTPUT_CLEANUP_FAILED", "output_cleanup",
                       "解压成功，产物清理失败：" + current.output["cleanup_error"], "cloud" if task.source == "115" and not task.policy.get("fallback_approved") else "local")
            raise
