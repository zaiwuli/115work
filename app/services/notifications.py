"""UnpackFlow-compatible MS payloads, templates and stage switches."""
from datetime import datetime
import re
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


DEFAULT_TEMPLATE = "{{icon}} {{title}}\n{{separator}}\n⏱️ 时间：{{time}}\n📦 来源：{{source}}\n📄 任务：{{task}}"

STAGE_TITLES = {
    "cloud_prepare": "准备云解压", "cloud_inspect": "检查压缩包内容",
    "cloud_output": "云端导出解压文件", "cloud_export_unknown": "云解压结果等待核对",
    "cloud_progress": "云端解压中", "cloud_verify": "确认云端完成",
    "cloud_password": "等待压缩密码", "local_fallback": "等待确认本地解压",
    "local_extract": "本地解压中", "output_cleanup": "清理解压产物",
    "output_flatten": "同名文件夹提层", "registered": "文件登记",
    "complete": "处理完成", "cache": "缓存压缩包",
}


def normalize_ms_url(raw):
    parsed = urlsplit(raw.strip())
    endpoint = "/api/v1/message/openSend"
    path = parsed.path.rstrip("/")
    if not path.endswith(endpoint):
        path += endpoint
    return urlunsplit(parsed._replace(path=path))


def notification_stage(event):
    if event.kind == "SEED_CLEANUP_FAILED":
        return "attention"
    if event.kind == "SEED_CLEANUP_COMPLETED":
        return "cleanup"
    if event.kind in {"FAILED", "SUBMIT_FAILED", "SUBMISSION_UNKNOWN",
                      "POSTPROCESS_FAILED", "OUTPUT_CLEANUP_FAILED",
                      "OUTPUT_FLATTEN_FAILED", "WAITING_APPROVAL", "RECONCILING"}:
        return "attention"
    if event.kind in {"SUBMITTED", "SUCCESS", "SUCCEEDED"}:
        return "complete"
    if event.kind == "DISCOVERED":
        return "discovery"
    if any(part in event.stage for part in ("cleanup", "postprocess", "delete", "retention")):
        return "cleanup"
    if event.kind in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
        return "complete"
    if any(part in event.stage for part in ("cache", "download", "transfer")):
        return "cache"
    return "extract"


def notification_group(event):
    if event.kind == "WAITING_RETRY" and event.stage == "cloud_prepare":
        return "cloud_failure"
    if event.kind in {"DOWNLOADING", "UNKNOWN", "DOWNLOAD_COMPLETED", "CANCELED", "OUTPUT_CLEANUP_SUCCEEDED", "SEED_CLEANUP_STARTED"}:
        return event.kind.lower()
    if event.kind in {"SEED_CLEANUP_COMPLETED", "SEED_CLEANUP_FAILED"}:
        return event.kind.lower()
    if event.kind == "TEST":
        return "test"
    group = notification_stage(event)
    if group == "attention":
        return "attention:" + event.kind + ":" + event.stage
    if event.kind == "DISCOVERED":
        return "discovery"
    if event.kind in {"SUBMITTED", "SUCCESS", "SUCCEEDED", "POSTPROCESS_SUCCEEDED"}:
        return event.kind.lower()
    if event.kind == "RUNNING" and event.stage in {"cache", "local_extract", "cloud_progress"}:
        return group
    if event.kind == "RUNNING" and event.stage == "document_copy":
        return "cache"
    return ""


def enabled(settings, event, category):
    if event.kind == "TEST":
        return True
    if settings.get("notification_events") is not None:
        from app.services.notification_options import event_option
        return bool(settings.get("notification_enabled", True) and
                    settings["notification_events"].get(event_option(event, category), False))
    if not notification_group(event):
        return False
    if not settings.get("notification_enabled", True):
        return False
    if notification_stage(event) == "attention":
        return settings.get("notification_" + category, True)
    return settings.get("notification_" + category, True) and settings.get(
        "notification_" + notification_stage(event), True,
    )


def ms_payload(settings, event, category, task_name):
    titles = {"DISCOVERED": "发现压缩包", "SUCCEEDED": "解压完成", "FAILED": "解压失败",
              "RUNNING": "开始解压", "IMPORT": "离线导入汇总", "TEST": "测试通知",
              "SUBMITTED": "离线任务已提交", "SUCCESS": "离线任务完成",
              "POSTPROCESS_SUCCEEDED": "任务清理完成", "POSTPROCESS_FAILED": "任务清理失败"}
    titles["CLOUD_TAKEOVER"] = "云解压已接管"
    titles.update({"SEED_CLEANUP_COMPLETED": "种子下载后清理完成",
                   "SEED_CLEANUP_FAILED": "种子下载成功，清理失败"})
    if event.kind == "SEED_CLEANUP_COMPLETED" and "仅预演" in event.message:
        titles[event.kind] = "种子清理预演完成（未删除文件）"
    stage = getattr(event, "stage", "")
    title = titles.get(event.kind, STAGE_TITLES.get(stage, "任务状态更新"))
    if event.kind == "TEST" and stage == "notification_flow_test":
        title = event.message
    if event.kind not in titles and event.message and not re.fullmatch(r"[A-Za-z0-9_ :.-]+", event.message):
        title = event.message
    if settings.get("notification_events") is not None:
        from app.services.notification_options import CATALOG, event_option
        option = event_option(event, category)
        title = next((item["label"] for item in CATALOG if item["id"] == option), title)
        if option == "seed.preview":
            title = "种子清理预演完成（未删除文件）"
    values = {
        "icon": {
            "DISCOVERED": "📦", "RUNNING": "⏱️", "SUCCEEDED": "✅",
            "FAILED": "❌", "POSTPROCESS_SUCCEEDED": "🧹",
            "POSTPROCESS_FAILED": "❌", "SUBMITTED": "📥",
            "SUCCESS": "✅", "IMPORT": "📋", "TEST": "🔔",
            "CLOUD_TAKEOVER": "☁️",
        }.get(event.kind, "⚪"),
        "title": title, "separator": "--------------------",
        "time": datetime.fromtimestamp(event.created_at, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"cloud": "115 云解压", "offline": "115 离线", "local": "本地处理"}.get(category, category),
        "task": task_name or event.message,
        "stage": STAGE_TITLES.get(stage, "处理中"),
    }
    # One substitution pass; path-embedded passwords never enter notifications.
    values["task"] = re.sub(r"\{\{[^}]*\}\}", "{{***}}", values["task"])
    content = re.sub(r"\{\{(\w+)\}\}", lambda match: values.get(match[1], match[0]),
                     settings.get("notification_template") or DEFAULT_TEMPLATE)
    return {"title": title, "content": content, "imageUrl": "", "proxy": False}


def webhook_payload(settings, event, category, task_name, state=""):
    rendered = ms_payload(settings, event, category, task_name)
    return {
        "event_id": event.id, "event": event.kind, "task_id": event.task_id,
        "task_name": re.sub(r"\{\{[^}]*\}\}", "{{***}}", task_name),
        "category": category, "stage": event.stage, "state": state,
        "title": rendered["title"], "content": rendered["content"],
        "message": event.message, "timestamp": event.created_at,
    }
