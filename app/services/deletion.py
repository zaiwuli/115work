from sqlalchemy import delete, select

from app.persistence.models import (
    Batch, BatchItem, CacheEntry, DocumentImport, Event, Identity, Job, Locator,
    Offline, OfflineExecution, Operation, Outbox, Resource, ResourceLink, Settings,
    Step, Task, Transfer,
)


def remove_events(session, identifier):
    events = select(Event.id).where(Event.task_id == identifier)
    session.execute(delete(Outbox).where(Outbox.event_id.in_(events)))
    session.execute(delete(Event).where(Event.task_id == identifier))


def delete_task(session, identifier):
    task = session.get(Task, identifier)
    if not task:
        raise ValueError("TASK_NOT_FOUND")
    if task.state not in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
        raise ValueError("TERMINAL_TASK_REQUIRED")
    jobs = [job for job in session.scalars(select(Job))
            if job.payload.get("task_id") == identifier]
    if any(job.state in {"RUNNING", "READY", "RECONCILING"} for job in jobs):
        raise ValueError("TASK_OPERATIONS_PENDING")
    active = session.get(Settings, "cloud_active")
    if active and active.value.get("value") == identifier:
        raise ValueError("TASK_OPERATIONS_PENDING")
    if session.scalar(select(Operation.key).where(
        Operation.task_id == identifier, Operation.status.in_(["UNKNOWN", "PREPARED"]),
    )):
        raise ValueError("TASK_OPERATIONS_PENDING")
    for model in (Identity, ResourceLink, Step, Transfer, CacheEntry, Operation):
        session.execute(delete(model).where(model.task_id == identifier))
    resources = select(Resource.id).where(Resource.task_id == identifier)
    session.execute(delete(Locator).where(Locator.resource_id.in_(resources)))
    session.execute(delete(Resource).where(Resource.task_id == identifier))
    for job in jobs:
        session.delete(job)
    remove_events(session, identifier)
    session.delete(task)


def delete_offline(session, identifier):
    task = session.get(Offline, identifier)
    if not task:
        raise ValueError("OFFLINE_TASK_NOT_FOUND")
    if task.status in {"submitting", "submission_unknown"}:
        raise ValueError("OFFLINE_OPERATION_PENDING")
    jobs = [job for job in session.scalars(select(Job))
            if job.payload.get("batch_id") == task.batch_id]
    if any(job.state == "RUNNING" for job in jobs):
        raise ValueError("OFFLINE_OPERATION_PENDING")
    affected_batches = list(session.scalars(select(BatchItem.batch_id).where(
        BatchItem.task_id == identifier,
    )))
    session.execute(delete(BatchItem).where(BatchItem.task_id == identifier))
    session.execute(delete(ResourceLink).where(ResourceLink.offline_id == identifier))
    session.execute(delete(OfflineExecution).where(OfflineExecution.offline_id == identifier))
    session.execute(delete(Operation).where(Operation.task_id == identifier))
    remove_events(session, identifier)
    for window in session.scalars(select(Settings).where(Settings.key.startswith("offline-window:"))):
        ids = [x for x in window.value["ids"] if x != identifier]
        if ids:
            window.value = window.value | {"ids": ids}
        else:
            session.delete(window)
    session.delete(task)
    session.flush()
    for batch_id in set(affected_batches + [task.batch_id]):
        if session.scalar(select(Offline.id).where(Offline.batch_id == batch_id)) or session.scalar(
            select(BatchItem.id).where(BatchItem.batch_id == batch_id)
        ):
            continue
        for job in session.scalars(select(Job)):
            if job.payload.get("batch_id") == batch_id:
                session.delete(job)
        session.execute(delete(DocumentImport).where(DocumentImport.batch_id == batch_id))
        remove_events(session, batch_id)
        session.execute(delete(Settings).where(Settings.key == "batch-owner:" + batch_id))
        batch = session.get(Batch, batch_id)
        if batch:
            session.delete(batch)
