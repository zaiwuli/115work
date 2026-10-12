import time
import re

from sqlalchemy import select

from app.persistence.models import Event, Locator, Outbox, Resource, Settings, Step
from app.domain.state import validate_transition


def record(session, task_id: str, kind: str, stage: str, message: str, category: str = ""):
    message = re.sub(r"\{\{[^}]*\}\}", "{{***}}", message)
    event = Event(task_id=task_id, kind=kind, stage=stage, message=message)
    session.add(event)
    session.flush()
    if category:
        enabled = session.get(Settings, "notification_" + category)
        if kind == "TEST" or enabled is None or enabled.value["value"]:
            session.add(Outbox(event_id=event.id, category=category))
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
    record(session, task.id, state, stage, error or stage,
           "cloud" if task.source == "115" else "local")


def track_step(session, task, prior_stage, state, stage, error=""):
    previous = session.scalar(select(Step).where(Step.task_id == task.id, Step.kind == prior_stage)
                              .order_by(Step.updated_at.desc()))
    if previous and prior_stage != stage and previous.state not in {"FAILED", "CANCELED"}:
        previous.state, previous.updated_at = ("FAILED" if error else "SUCCEEDED"), time.time()
    key = f"{task.id}:{stage}:{task.attempts}"
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
