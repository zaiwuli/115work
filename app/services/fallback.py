from app.persistence.models import Settings
from app.scheduler.store import enqueue
from app.services.events import record, transition


def can_download_local(task):
    return bool(
        task.source == "115" and task.locator.get("pickcode")
        and not task.cancel_requested and not task.policy.get("fallback_approved")
        and task.state not in {"SUCCEEDED", "CANCELED", "SKIPPED"}
        and (task.stage.startswith("cloud_") or task.stage == "local_fallback")
        and task.output.get("failure_archive_result") not in {"PENDING", "FAILED"}
    )


def expire_cloud(session, task):
    task.policy = task.policy | {
        "cloud_remote_unsettled": bool(task.remote_id) or task.stage in {
            "cloud_output", "cloud_export_unknown",
        },
    }
    transition(session, task, "WAITING_APPROVAL", "local_fallback",
               error="CLOUD_PROCESSING_TIMEOUT")
    task.wait_reason = "APPROVAL"
    active = session.get(Settings, "cloud_active")
    if active and active.value.get("value") == task.id:
        active.value = {"value": ""}
    record(session, task.id, "CLOUD_TIMEOUT", "local_fallback",
           "云处理超过设定期限，已释放本工具云队列，可选择下载到本地解压；远端操作可能继续")
    if not task.policy.get("cloud_remote_unsettled"):
        approve_automatic(session, task)


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
