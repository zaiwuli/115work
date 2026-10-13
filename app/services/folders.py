from app.adapters.p115 import AdapterError
from app.persistence.models import Operation
from sqlalchemy import select


async def output_folder(db, adapter, parent, name, key, task_id):
    with db.session() as session:
        operation = session.get(Operation, key)
    if operation:
        return await durable_folder(db, adapter, parent, operation.evidence["name"], key, task_id)
    offset, occupied = 0, set()
    with db.session() as session:
        occupied.update(operation.evidence.get("name", "").casefold()
                        for operation in session.scalars(select(Operation).where(
                            Operation.kind == "folder_create",
                        )) if operation.evidence.get("parent") == parent)
    for _ in range(100):
        page = await adapter.files(parent, offset)
        occupied.update(file.name.casefold() for file in page.files)
        if page.next_offset is None:
            break
        offset = page.next_offset
    else:
        raise AdapterError("FOLDER_LOOKUP_BUDGET")
    candidate = name
    if candidate.casefold() in occupied:
        candidate = f"{name}-{task_id[:10]}"
        number = 2
        while candidate.casefold() in occupied:
            candidate = f"{name}-{task_id[:10]}-{number}"
            number += 1
    return await durable_folder(db, adapter, parent, candidate, key, task_id)


async def durable_folder(db, adapter, parent, name, key, task_id=""):
    with db.session() as session:
        operation = session.get(Operation, key)
        if operation and operation.status == "COMPLETED":
            return operation.evidence["cid"]
        recovering = operation is not None
        if not operation:
            session.add(Operation(key=key, task_id=task_id, kind="folder_create",
                                  evidence={"parent": parent, "name": name}))
    if recovering:
        offset, matches = 0, []
        for _ in range(100):
            page = await adapter.files(parent, offset)
            matches.extend(file for file in page.files if file.is_dir and file.name == name)
            if page.next_offset is None:
                break
            offset = page.next_offset
        else:
            raise AdapterError("FOLDER_LOOKUP_BUDGET")
        if len(matches) != 1:
            raise AdapterError("FOLDER_CREATE_REQUIRES_RECONCILIATION", uncertain=True)
        cid = matches[0].fid
    else:
        try:
            cid = await adapter.folder(parent, name)
        except AdapterError as exc:
            if not exc.uncertain:
                with db.session() as session:
                    session.delete(session.get(Operation, key))
            raise
    with db.session() as session:
        operation = session.get(Operation, key)
        operation.status = "COMPLETED"
        operation.evidence = operation.evidence | {"cid": cid}
    return cid
