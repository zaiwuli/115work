from sqlalchemy import func, select

from app.persistence.models import Event


def operation_scope(task):
    generation = task.policy.get("execution_generation", 0)
    return f"{task.id}:run{generation}" if generation else task.id


def begin_execution(session, task):
    # Keep legacy operation keys valid until the user starts a new execution.
    cutoff = session.scalar(select(func.max(Event.id)).where(Event.task_id == task.id)) or 0
    task.policy = task.policy | {
        "execution_generation": task.policy.get("execution_generation", 0) + 1,
        "execution_event_cutoff": cutoff,
        "delete_approved": False,
    }
