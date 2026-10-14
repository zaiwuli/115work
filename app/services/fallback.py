from app.persistence.models import Settings
from app.scheduler.store import enqueue
from app.services.events import record, transition


def can_fallback(task):
    return bool(task.source == "115" and task.locator.get("pickcode") and (
        (task.state == "WAITING_APPROVAL" and task.stage == "local_fallback") or
        (task.state == "FAILED" and task.stage in {
            "cloud_prepare", "cloud_inspect", "cloud_output", "cloud_progress", "cloud_verify",
        } and task.error in {"REMOTE_REJECTED", "CLOUD_CONTENT_FAILED", "CLOUD_EXPORT_REJECTED"})
    ) and task.output.get("failure_archive_result") not in {"PENDING", "FAILED"})


def approve_automatic(session, task):
    if not task.policy.get("auto_fallback") or task.policy.get("fallback_approved") or not can_fallback(task):
        return
    transition(session, task, "READY", "cache", manual_retry=task.state == "FAILED")
    task.completed_at = None
    task.policy = task.policy | {"fallback_approved": True}
    active = session.get(Settings, "cloud_active")
    if active and active.value.get("value") == task.id:
        active.value = {"value": ""}
    enqueue(session, "local:" + task.id, "local_extract", {"task_id": task.id})
    record(session, task.id, "FALLBACK_AUTHORIZED", "cache", "按处理规则批准下载原包并本地解压")
