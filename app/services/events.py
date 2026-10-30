import time
import re

from sqlalchemy import select

from app.persistence.models import Event, Locator, Outbox, Resource, Settings, Step, Task, Offline
from app.domain.state import validate_transition


def record(session, task_id: str, kind: str, stage: str, message: str, category: str = ""):
    message = re.sub(r"\{\{[^}]*\}\}", "{{***}}", message)
    event = Event(task_id=task_id, kind=kind, stage=stage, message=message)
    session.add(event)
    session.flush()
    if category:
        from app.services.notifications import notification_group
        group = notification_group(event)
        mode = session.get(Settings, "notification_mode")
        simplified = mode is not None and mode.value.get("value") != "custom"
        if simplified and kind in {"WAITING_APPROVAL", "RECONCILING"} and stage == "local_fallback":
            group = "fallback_attention"
        if not group:
            return event
        if kind != "TEST" and not kind.startswith("CHAIN_"):
            prior = select(Event).join(Outbox, Outbox.event_id == Event.id).where(
                Event.task_id == task_id, Event.id != event.id,
                Outbox.category == category,
            )
            task = session.get(Task, task_id)
            if task:
                prior = prior.where(Event.id > task.policy.get("execution_event_cutoff", 0))
            offline = session.get(Offline, task_id) if category == "offline" else None
            if offline:
                cutoff = session.get(Settings, "offline-notification-cutoff:" + task_id)
                if kind.startswith("SEED_CLEANUP_"):
                    seed = session.get(Settings, "seed-policy:" + task_id)
                    if seed:
                        prior = prior.where(Event.id > seed.value.get("notification_cutoff", 0))
                if cutoff:
                    prior = prior.where(Event.id > cutoff.value["id"])
            def previous_group(previous):
                if simplified and previous.kind in {"WAITING_APPROVAL", "RECONCILING"} and previous.stage == "local_fallback":
                    return "fallback_attention"
                return notification_group(previous)
            if any(previous_group(previous) == group for previous in session.scalars(prior)):
                return event
        enabled = session.get(Settings, "notification_" + category)
        options = session.get(Settings, "notification_events")
        mode = session.get(Settings, "notification_mode")
        if kind == "TEST" or mode is not None or options is not None or enabled is None or enabled.value["value"]:
            status = "pending"
            if options is not None or mode is not None:
                from app.services.notifications import enabled as notification_enabled
                values = {row.key: row.value["value"] for row in session.scalars(select(Settings))
                          if row.key.startswith("notification_") and "value" in row.value}
                if not notification_enabled(values, event, category):
                    status = "suppressed"
            session.add(Outbox(event_id=event.id, category=category, status=status))
    return event


def transition(session, task, state: str, stage: str, *, error: str = "", manual_retry=False):
    validate_transition(task.state, state, manual_retry=manual_retry)
    if task.state == state and task.stage == stage and task.error == error:
        return
    prior_stage = task.stage
    task.state, task.stage, task.error, task.updated_at = state, stage, error, time.time()
    track_step(session, task, prior_stage, state, stage, error)
    if state in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
        task.completed_at = time.time()
    category = "cloud" if task.source == "115" and not task.policy.get("fallback_approved") else "local"
    if state == "SUCCEEDED" and (task.policy.get("cleanup_enabled") or (
        task.source == "115" and task.policy.get("squash_root", True)
        and task.output.get("verification") in {"remote_manifest", "remote_reported_complete"}
    )):
        category = ""
    record(session, task.id, state, stage, error or stage, category)


def track_step(session, task, prior_stage, state, stage, error=""):
    previous = session.scalar(select(Step).where(Step.task_id == task.id, Step.kind == prior_stage)
                              .order_by(Step.updated_at.desc()))
    if previous and prior_stage != stage and previous.state not in {"FAILED", "CANCELED"}:
        previous.state, previous.updated_at = ("FAILED" if error else "SUCCEEDED"), time.time()
    from app.services.execution import operation_scope
    key = f"{operation_scope(task)}:{stage}:{task.attempts}"
    step = session.scalar(select(Step).where(Step.operation_key == key))
    if not step:
        step = Step(operation_key=key, task_id=task.id, kind=stage,
                    dependencies=[previous.id] if previous and prior_stage != stage else [])
        session.add(step)
    step.state, step.error, step.attempts = state, error, task.attempts
    step.result, step.updated_at = task.output, time.time()
    resource = session.scalar(select(Resource).where(Resource.object_key == task.object_key))
    if not resource:
        resource = Resource(object_key=task.object_key, name=task.name, size=task.size,
                            task_id=task.id, version=task.locator, strong_hash=task.locator.get("sha1", ""))
        session.add(resource)
        session.flush()
        session.add(Locator(resource_id=resource.id, connection_id=task.source,
                            kind=task.source, evidence=task.locator))
