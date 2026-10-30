"""Summarize persisted parent/child outcomes, never intermediate success events."""
from sqlalchemy import select

from app.persistence.models import Event, Offline, ResourceLink, Settings, Task
from app.services.events import record


def outcome(task):
    if task.state in {"CANCELED", "SKIPPED"}:
        return "canceled"
    if task.state == "FAILED" or any(task.output.get(key) == "FAILED" for key in (
        "cleanup_result", "flatten_result", "postprocess_result",
    )):
        return "failed"
    if task.state != "SUCCEEDED":
        return "pending"
    needs_post = task.policy.get("original_action", "retain") != "retain" or task.policy.get("cleanup_enabled")
    needs_post = needs_post or (task.source == "115" and not task.output.get("path")
                               and task.policy.get("squash_root", True))
    if needs_post and task.output.get("postprocess_result") != "SUCCEEDED":
        return "pending"
    return "success"


def collect(session, settings):
    if settings.get("notification_mode", "custom") == "custom":
        return
    since = settings.get("notification_mode_since", 0)
    tasks = {t.id: t for t in session.scalars(select(Task))}
    parents = {}
    for link in session.scalars(select(ResourceLink)):
        parents.setdefault(link.offline_id, []).append(tasks[link.task_id])
    linked = {t.id for children in parents.values() for t in children}

    def publish(identifier, category, states, message, cutoff=0):
        if not states or "pending" in states:
            return
        kind = "CHAIN_FAILED" if "failed" in states else "CHAIN_SUCCEEDED"
        if "canceled" in states and kind != "CHAIN_FAILED":
            return
        # Require an actual business event after enabling the plan; never replay old history.
        recent = session.scalar(select(Event.id).where(
            Event.task_id.in_([identifier] + [t.id for t in parents.get(identifier, [])]),
            Event.created_at >= since, Event.id > cutoff,
            Event.kind.not_like("CHAIN_%"),
            Event.kind.in_(["SUCCESS", "SUCCEEDED", "FAILED", "SUBMIT_FAILED", "SEED_CLEANUP_COMPLETED",
                           "SEED_CLEANUP_FAILED", "POSTPROCESS_SUCCEEDED", "POSTPROCESS_FAILED",
                           "OUTPUT_CLEANUP_FAILED", "OUTPUT_FLATTEN_FAILED"]),
        ).limit(1))
        if not recent:
            return
        previous = session.scalar(select(Event.id).where(
            Event.task_id == identifier, Event.kind == kind, Event.id > cutoff,
        ))
        if previous:
            return
        if not settings.get("notification_enabled", True):
            # Consume this outcome while muted so enabling notifications cannot replay it.
            record(session, identifier, kind, "complete", message)
            return
        record(session, identifier, kind, "complete", message, category)

    for item in session.scalars(select(Offline)):
        seed = session.get(Settings, "seed-policy:" + item.id)
        children = parents.get(item.id, [])
        child_states = [outcome(t) for t in children]
        states = list(child_states)
        if item.status in {"failed", "submit_failed"}:
            states = ["failed"]
        elif item.status != "success":
            continue
        elif seed:
            result = seed.value.get("result")
            if result not in {"SUCCEEDED", "PREVIEW", "FAILED"}:
                continue
            states.append("failed" if result == "FAILED" else "success")
        elif not children:
            if item.stage != "complete":
                continue
            states = ["success"]
        message = "下载失败：" + item.error if item.status in {"failed", "submit_failed"} else "下载完成"
        if seed:
            result = seed.value.get("result")
            message += "，清理失败" if result == "FAILED" else "，清理预演完成（未删除文件）" if result == "PREVIEW" else "，清理完成"
        if children:
            message += f"；关联处理共 {len(children)} 个，成功 {child_states.count('success')}，失败 {child_states.count('failed')}"
            failed = [t.name for t in children if outcome(t) == "failed"]
            if failed:
                message += "；失败项：" + "、".join(failed[:10])
        cutoff = session.get(Settings, "offline-notification-cutoff:" + item.id)
        boundary = cutoff.value["id"] if cutoff else 0
        if seed:
            boundary = max(boundary, seed.value.get("notification_cutoff", 0))
        boundary = max([boundary] + [t.policy.get("execution_event_cutoff", 0) for t in children])
        publish(item.id, "offline", states, message, boundary)
    for task in tasks.values():
        if task.id in linked:
            continue
        state = outcome(task)
        message = "解压及已启用的后处理完成" if state == "success" else "处理失败：" + (task.error or task.output.get("cleanup_error") or task.output.get("postprocess_result", "请查看任务详情"))
        publish(task.id, "local" if task.source != "115" or task.policy.get("fallback_approved") else "cloud",
                [state], message, task.policy.get("execution_event_cutoff", 0))
