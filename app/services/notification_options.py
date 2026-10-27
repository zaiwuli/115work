"""Public stage catalog shared by settings UI and notification routing."""
GROUPS = [
    ("offline", "离线 / 种子下载", [("submitted", "已提交下载"), ("started", "开始下载"), ("success", "下载成功"), ("failed", "下载失败"), ("unknown", "下载状态待核对")]),
    ("seed", "种子清理", [("preview", "预演完成"), ("started", "开始清理"), ("success", "清理完成"), ("failed", "清理失败")]),
    ("cloud", "云端解压（含种子内压缩包）", [("discovery", "发现压缩包"), ("started", "开始云解压"), ("success", "云解压成功"), ("failed", "云解压失败"), ("timeout", "云处理超时"), ("password", "等待密码"), ("unknown", "等待结果核对")]),
    ("local", "本地处理", [("download_started", "开始下载原包"), ("download_success", "原包下载完成"), ("download_failed", "原包下载失败"), ("started", "开始本地解压"), ("success", "本地解压成功"), ("failed", "本地解压失败")]),
    ("post", "完成后处理", [("success", "原包处理成功"), ("failed", "原包处理失败"), ("cleanup_success", "产物清理完成"), ("cleanup_failed", "产物清理失败")]),
    ("control", "任务控制", [("canceled", "已取消任务")]),
]
CATALOG = [{"id": group + "." + key, "label": label, "group": group, "group_label": title,
            "result": key.endswith(("success", "failed")) or key in {"preview", "timeout", "password", "unknown"},
            "attention": key.endswith("failed") or key in {"timeout", "password", "unknown"}}
           for group, title, entries in GROUPS for key, label in entries]
KEYS = {x["id"] for x in CATALOG}


def event_option(event, category):
    kind, stage = event.kind, event.stage
    if kind == "TEST":
        return "test"
    if kind == "CANCELED":
        return "control.canceled"
    seed = {"SEED_CLEANUP_STARTED": "started", "SEED_CLEANUP_FAILED": "failed", "SEED_CLEANUP_COMPLETED": "preview" if "仅预演" in event.message else "success"}
    if kind in seed:
        return "seed." + seed[kind]
    if kind in {"POSTPROCESS_SUCCEEDED", "POSTPROCESS_FAILED"}:
        return "post." + ("success" if kind.endswith("SUCCEEDED") else "failed")
    if kind in {"OUTPUT_CLEANUP_SUCCEEDED", "OUTPUT_CLEANUP_FAILED", "OUTPUT_FLATTEN_FAILED"}:
        return "post.cleanup_" + ("success" if kind.endswith("SUCCEEDED") else "failed")
    if category == "offline":
        return {"SUBMITTED": "offline.submitted", "DOWNLOADING": "offline.started", "SUCCESS": "offline.success", "FAILED": "offline.failed", "SUBMIT_FAILED": "offline.failed", "SUBMISSION_UNKNOWN": "offline.unknown", "UNKNOWN": "offline.unknown"}.get(kind, "")
    if kind == "DOWNLOAD_COMPLETED":
        return "local.download_success"
    if stage in {"cache", "document_copy"}:
        return {"RUNNING": "local.download_started", "FAILED": "local.download_failed"}.get(kind, "")
    if kind == "DISCOVERED":
        return "cloud.discovery" if category == "cloud" else ""
    if stage == "local_extract":
        return {"RUNNING": "local.started", "SUCCEEDED": "local.success", "FAILED": "local.failed"}.get(kind, "")
    if kind == "SUCCEEDED":
        return "local.success" if category == "local" else "cloud.success"
    if getattr(event, "message", "") == "CLOUD_PROCESSING_TIMEOUT" or kind == "CLOUD_TIMEOUT":
        return "cloud.timeout"
    if stage == "cloud_password" and kind == "WAITING_APPROVAL":
        return "cloud.password"
    if (kind == "WAITING_RETRY" and stage == "cloud_prepare") or (kind == "WAITING_APPROVAL" and stage == "local_fallback"):
        return "cloud.failed"
    if kind in {"RECONCILING", "WAITING_APPROVAL"}:
        return "cloud.unknown"
    return {"RUNNING": "cloud.started", "FAILED": "cloud.failed"}.get(kind, "")


def effective_options(settings):
    configured = settings.get("notification_events")
    if configured is not None:
        return configured
    result = {}
    for item in CATALOG:
        group = item["group"]
        category = "offline" if group in {"offline", "seed"} else "local" if group == "local" else "cloud"
        stage = "cleanup" if group in {"post", "seed"} else "discovery" if item["id"].endswith("discovery") else "cache" if "download_" in item["id"] else "extract" if item["id"].endswith("started") else "complete"
        result[item["id"]] = bool(settings.get("notification_" + category, True) and settings.get("notification_" + stage, True))
    return result
