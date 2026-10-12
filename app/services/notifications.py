"""UnpackFlow-compatible MS payloads, templates and stage switches."""
from datetime import datetime
import re
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


DEFAULT_TEMPLATE = "{{icon}} {{title}}\n{{separator}}\n⏱️ 时间：{{time}}\n📦 来源：{{source}}\n📄 任务：{{task}}"


def normalize_ms_url(raw):
    parsed = urlsplit(raw.strip())
    endpoint = "/api/v1/message/openSend"
    path = parsed.path.rstrip("/")
    if not path.endswith(endpoint):
        path += endpoint
    return urlunsplit(parsed._replace(path=path))


def notification_stage(event):
    if event.kind == "DISCOVERED":
        return "discovery"
    if any(part in event.stage for part in ("cleanup", "postprocess", "delete", "retention")):
        return "cleanup"
    if event.kind in {"SUCCEEDED", "FAILED", "CANCELED", "SKIPPED"}:
        return "complete"
    if any(part in event.stage for part in ("cache", "download", "transfer")):
        return "cache"
    return "extract"


def enabled(settings, event, category):
    if event.kind == "TEST":
        return True
    if not settings.get("notification_enabled", True):
        return False
    if category in {"offline", "cloud"}:
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
    title = titles.get(event.kind, event.message)
    values = {
        "icon": "!" if event.kind == "FAILED" else "+",
        "title": title, "separator": "--------------------",
        "time": datetime.fromtimestamp(event.created_at, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
        "source": {"cloud": "115 云解压", "offline": "115 离线", "local": "本地处理"}.get(category, category),
        "task": task_name or event.message,
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
