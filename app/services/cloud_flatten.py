from pathlib import PurePosixPath

from app.persistence.models import Operation, Task
from app.services.events import record
from app.services.execution import operation_scope


async def listing(adapter, cid):
    result, offset = [], 0
    for _ in range(100):
        page = await adapter.files(cid, offset)
        result.extend(page.files)
        if page.next_offset is None:
            return result
        if page.next_offset <= offset:
            break
        offset = page.next_offset
    raise ValueError("OUTPUT_MANIFEST_PAGE_LIMIT")


async def flatten(db, store, adapter, task, job):
    if not task.policy.get("squash_root", True) or task.output.get("path"):
        return
    if task.output.get("flatten_result") == "SUCCEEDED":
        return
    root = task.output["cid"]
    name = PurePosixPath(task.name).stem[:80]
    try:
        if task.state != "SUCCEEDED" or task.cancel_requested or task.output.get("verification") not in {
            "remote_manifest", "remote_reported_complete",
        }:
            raise ValueError("VERIFIED_OUTPUT_REQUIRED")
        for depth in range(10):
            key = f"flatten:{operation_scope(task)}:{depth}"
            with db.session() as session:
                store.owned(session, job)
                operation = session.get(Operation, key)
                evidence = operation.evidence if operation else None
                if operation and operation.status == "COMPLETED":
                    continue
            if evidence is None:
                outer = await listing(adapter, root)
                if len(outer) != 1 or not outer[0].is_dir or outer[0].name != name:
                    break
                folder = outer[0]
                folders = [folder]
                children = await listing(adapter, folder.fid)
                for _ in range(10):
                    if len(children) != 1 or not children[0].is_dir or children[0].name != name:
                        break
                    folder = children[0]
                    folders.append(folder)
                    children = await listing(adapter, folder.fid)
                else:
                    raise ValueError("OUTPUT_FLATTEN_DEPTH_LIMIT")
                if any(child.name == name for child in children):
                    raise ValueError("OUTPUT_NAME_CONFLICT")
                evidence = {"folder": {"fid": folder.fid, "cid": root,
                                       "size": folder.size, "sha1": folder.sha1},
                            "folders": [{"fid": x.fid,
                                         "cid": root if i == 0 else folders[i - 1].fid,
                                         "size": x.size, "sha1": x.sha1}
                                        for i, x in enumerate(folders)],
                            "children": [{"fid": x.fid, "cid": folder.fid,
                                          "size": x.size, "sha1": x.sha1} for x in children]}
                with db.session() as session:
                    session.add(Operation(key=key, task_id=task.id, kind="output_flatten",
                                          evidence=evidence))
            for child in evidence["children"]:
                # A durable per-move marker prevents blindly repeating an uncertain mutation.
                move_key = key + ":" + child["fid"]
                with db.session() as session:
                    store.owned(session, job)
                    move = session.get(Operation, move_key)
                    if move and move.status == "COMPLETED":
                        continue
                    if move:
                        raise ValueError("FLATTEN_REQUIRES_RECONCILIATION")
                    session.add(Operation(key=move_key, task_id=task.id,
                                          kind="output_flatten_move", evidence=child))
                await adapter.move_verified(child, root)
                moved = await listing(adapter, root)
                if not any(x.fid == child["fid"] for x in moved):
                    raise ValueError("FLATTEN_REQUIRES_RECONCILIATION")
                with db.session() as session:
                    session.get(Operation, move_key).status = "COMPLETED"
            for folder in reversed(evidence.get("folders", [evidence["folder"]])):
                outer = await listing(adapter, folder["cid"])
                if any(x.fid == folder["fid"] for x in outer):
                    if await listing(adapter, folder["fid"]):
                        raise ValueError("OUTPUT_FOLDER_NOT_EMPTY")
                    await adapter.delete_verified(folder)
                if any(x.fid == folder["fid"] for x in await listing(adapter, folder["cid"])):
                    raise ValueError("FLATTEN_REQUIRES_RECONCILIATION")
            with db.session() as session:
                session.get(Operation, key).status = "COMPLETED"
                current = session.get(Task, task.id)
                prefixes = ["/".join([name] * count) + "/" for count in range(len(evidence.get("folders", [])), 0, -1)]
                def remap(entries):
                    result = []
                    for entry in entries:
                        path = next((entry["path"][len(prefix):] for prefix in prefixes
                                     if entry["path"].startswith(prefix)), entry["path"])
                        result.append(entry | {"path": path} | (
                            {"cid": root} if "/" not in path and "cid" in entry else {}))
                    return result
                current.output = current.output | {
                    "expected_manifest": remap(current.output.get("expected_manifest") or []),
                    **({"actual_manifest": remap(current.output["actual_manifest"])}
                       if "actual_manifest" in current.output else {}),
                }
                record(session, task.id, "OUTPUT_ROOT_FLATTENED", "output_flatten",
                       "同名文件夹已提层：" + name)
        else:
            raise ValueError("OUTPUT_FLATTEN_DEPTH_LIMIT")
        with db.session() as session:
            current = session.get(Task, task.id)
            current.output = current.output | {"flatten_result": "SUCCEEDED",
                                               "flatten_error": ""}
    except Exception as exc:
        with db.session() as session:
            current = session.get(Task, task.id)
            current.output = current.output | {"flatten_result": "FAILED",
                                               "flatten_error": str(exc) if isinstance(exc, ValueError)
                                               else type(exc).__name__, "postprocess_result": "FAILED"}
            record(session, task.id, "OUTPUT_FLATTEN_FAILED", "output_flatten",
                   "解压成功，同名文件夹提层失败：" + current.output["flatten_error"])
        raise
