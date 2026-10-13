import asyncio
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol


class AdapterError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


@dataclass(frozen=True)
class CloudFile:
    fid: str
    cid: str
    name: str
    size: int
    pickcode: str = ""
    is_dir: bool = False
    sha1: str = ""


@dataclass(frozen=True)
class OfflineStatus:
    hash_value: str
    link: str
    name: str
    status: str


@dataclass(frozen=True)
class FilePage:
    files: list[CloudFile]
    next_offset: int | None


class P115Port(Protocol):
    async def folder(self, parent: str, name: str) -> str: ...
    async def submit(self, links: list[str], cid: str) -> None: ...
    async def offline_snapshot(self) -> list[OfflineStatus]: ...
    async def files(self, cid: str, offset: int = 0) -> FilePage: ...
    async def prepare_extract(self, pickcode: str, password: str = "") -> str: ...
    async def inspect_extract(self, pickcode: str) -> str: ...
    async def extract(self, pickcode: str, output_cid: str) -> str: ...
    async def extract_status(self, remote_id: str) -> str: ...


class DisabledAdapter:
    async def __getattribute_call(self, *args, **kwargs):
        raise AdapterError("AUTH_REQUIRED")

    folder = submit = offline_snapshot = files = prepare_extract = inspect_extract = (
        extract
    ) = extract_status = __getattribute_call


class P115Adapter:
    """Pinned upstream API, serialized requests and cached logical snapshots."""

    def __init__(self, cookie: str, timeout: float = 45):
        if not cookie:
            raise ValueError("COOKIE_REQUIRED")
        from p115client import P115Client

        self.client = P115Client(cookie, console_qrcode=False)
        self.timeout = timeout
        self.request_lock = asyncio.Lock()
        self.snapshot_lock = asyncio.Lock()
        self.folder_lock = asyncio.Lock()
        self.cache: dict = {}
        self.next_request = 0.0
        self.governor = None
        self.metrics = {"requests": 0, "cache_hits": 0, "errors": 0}

    async def call(self, name: str, *args, mutation: bool = False, **kwargs) -> dict:
        if self.governor:
            try:
                self.governor.before()
            except ValueError:
                raise AdapterError("REMOTE_CIRCUIT_OPEN") from None
        async with self.request_lock:
            if self.governor:
                try:
                    self.governor.before()
                except ValueError:
                    raise AdapterError("REMOTE_CIRCUIT_OPEN") from None
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 1
            try:
                self.metrics["requests"] += 1
                response = await asyncio.wait_for(
                    getattr(self.client, name)(*args, async_=True, **kwargs), self.timeout
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Upstream exceptions can embed cookies and signed URLs.
                self.metrics["errors"] += 1
                if self.governor:
                    self.governor.failure("REMOTE_UNAVAILABLE")
                raise AdapterError("REMOTE_UNAVAILABLE", uncertain=mutation) from None
            if not isinstance(response, dict):
                raise AdapterError("INVALID_REMOTE_RESPONSE", uncertain=mutation)
            if response.get("state") is False:
                code = str(response.get("errno", response.get("errcode", "")))
                if code in {"99", "401", "911", "990009"}:
                    if self.governor:
                        self.governor.failure("AUTH_OR_CHALLENGE_REQUIRED")
                    raise AdapterError("AUTH_OR_CHALLENGE_REQUIRED")
                if code in {"429", "10001"}:
                    if self.governor:
                        self.governor.failure("REMOTE_RATE_LIMIT")
                    raise AdapterError("REMOTE_RATE_LIMIT")
                raise AdapterError("REMOTE_REJECTED")
            if self.governor:
                self.governor.success()
            return response

    async def files(self, cid: str, offset: int = 0) -> FilePage:
        response = await self.call(
            "fs_files", {"cid": cid, "cur": 1, "limit": 115, "offset": offset, "show_dir": 1}
        )
        raw = response.get("data")
        if not isinstance(raw, list):
            raise AdapterError("INVALID_FILE_LIST")
        # A nonexistent CID can silently be treated as root by the upstream API.
        path = response.get("path", [])
        if cid != "0" and (
            not path or str(path[-1].get("cid", path[-1].get("file_id", ""))) != cid
        ):
            raise AdapterError("CID_NOT_VERIFIED")
        files = [
            CloudFile(
                str(item.get("fid") or item.get("cid")),
                cid,
                str(item.get("n", "")),
                int(item.get("s") or 0),
                str(item.get("pc") or ""),
                not bool(item.get("fid")),
                str(item.get("sha") or item.get("sha1") or "").lower(),
            )
            for item in raw
        ]
        count = int(response.get("count", offset + len(raw)))
        return FilePage(files, offset + len(raw) if raw and offset + len(raw) < count else None)

    async def directory_path(self, cid: str) -> list[str]:
        response = await self.call("fs_files", {
            "cid": cid, "cur": 1, "limit": 1, "offset": 0, "show_dir": 1,
        })
        path = [str(item.get("cid", item.get("file_id", "")))
                for item in response.get("path", [])]
        if cid != "0" and (not path or path[-1] != cid):
            raise AdapterError("CID_NOT_VERIFIED")
        return path or ["0"]

    async def folder(self, parent: str, name: str) -> str:
        async with self.folder_lock:
            offset = 0
            while True:
                page = await self.files(parent, offset)
                matches = [f for f in page.files if f.is_dir and f.name == name]
                if len(matches) > 1:
                    raise AdapterError("AMBIGUOUS_FOLDER")
                if matches:
                    return matches[0].fid
                if page.next_offset is None:
                    break
                offset = page.next_offset
            response = await self.call("fs_mkdir", name, pid=parent, mutation=True)
            data = response.get("data", response)
            cid = str(data.get("cid") or data.get("file_id") or "")
            if not cid:
                raise AdapterError("FOLDER_CREATE_UNKNOWN", uncertain=True)
            return cid

    async def submit(self, links: list[str], cid: str) -> None:
        if not 0 < len(links) <= 10:
            raise ValueError("SUBMIT_BATCH_LIMIT_10")
        payload = {f"url[{i}]": value for i, value in enumerate(links)}
        payload["wp_path_id"] = cid
        # The pinned client handles its submission protocol; don't duplicate it.
        response = await self.call("clouddownload_task_add_urls", payload, mutation=True)
        results = response.get("result")
        if isinstance(results, list) and any(x.get("state") is False for x in results):
            raise AdapterError("PARTIAL_SUBMISSION_REQUIRES_RECONCILIATION", uncertain=True)

    async def offline_snapshot(self) -> list[OfflineStatus]:
        async with self.snapshot_lock:
            cached = self.cache.get("offline")
            if cached and time.monotonic() - cached[0] < 20:
                self.metrics["cache_hits"] += 1
                return cached[1]
            result = []
            for page in range(1, 101):
                response = await self.call(
                    "clouddownload_task_list", {"page": page, "page_size": 100}
                )
                data = response.get("tasks", response.get("data", []))
                if isinstance(data, dict):
                    data = data.get("tasks", data.get("list", []))
                if not isinstance(data, list):
                    raise AdapterError("INVALID_OFFLINE_LIST")
                for item in data:
                    status = int(item.get("status", 0))
                    state = "success" if status == 2 or str(item.get("percentDone")) == "100" else (
                        "failed" if status < 0 else "downloading"
                    )
                    result.append(OfflineStatus(
                        str(item.get("info_hash") or item.get("hash") or "").lower(),
                        str(item.get("url") or ""),
                        str(item.get("name") or item.get("file_name") or ""),
                        state,
                    ))
                if page >= int(response.get("page_count") or 1):
                    self.cache["offline"] = (time.monotonic(), result)
                    return result
            raise AdapterError("OFFLINE_PAGE_BUDGET_EXCEEDED")

    @staticmethod
    def preparation_state(response: dict) -> str:
        raw = response.get("data", {}).get("unzip_status")
        if raw is None:
            return "unknown"
        status = int(raw)
        return {
            0: "running", 1: "running", 2: "failed", 3: "failed",
            4: "success", 6: "password", 7: "failed",
        }.get(status, "unknown")

    async def prepare_extract(self, pickcode: str, password: str = "") -> str:
        return self.preparation_state(await self.call(
            "extract_push", {"pick_code": pickcode, "secret": password}, mutation=True
        ))

    async def inspect_extract(self, pickcode: str) -> str:
        return self.preparation_state(await self.call("extract_push_progress", pickcode))

    async def extract(self, pickcode: str, output_cid: str) -> str:
        response = await self.call("extract_file", pickcode, to_pid=output_cid, mutation=True)
        data = response.get("data", response)
        remote_id = str(data.get("extract_id") or response.get("extract_id") or "")
        if not remote_id:
            raise AdapterError("EXTRACT_SUBMISSION_UNKNOWN", uncertain=True)
        return remote_id

    async def extract_status(self, remote_id: str) -> str:
        response = await self.call("extract_progress", remote_id)
        data = response.get("data")
        if not isinstance(data, dict):
            return "unknown"
        if data.get("extract_status") in {-1, "-1"}:
            return "failed"
        if str(data.get("percent", data.get("progress", ""))) == "100":
            return "success"
        return "running" if data else "unknown"

    async def archive_manifest(self, pickcode):
        from app.domain.paths import safe_relative
        queue, result = [""], []
        while queue:
            path, marker = queue.pop(0), ""
            for _ in range(100):
                response = await self.call("extract_list", pickcode, path=path, next_marker=marker)
                data = response.get("data", {})
                if not isinstance(data.get("list"), list):
                    raise AdapterError("INVALID_ARCHIVE_MANIFEST")
                for entry in data["list"]:
                    name = entry.get("file_name", "")
                    relative = safe_relative(str(PurePosixPath(path) / name)).as_posix()
                    directory = int(entry.get("file_category", 1)) == 0
                    if directory:
                        queue.append(relative)
                    else:
                        size = entry.get("file_size", entry.get("size"))
                        result.append({"path": relative, "size": int(size) if size is not None else None})
                    if len(result) + len(queue) > 10000:
                        raise AdapterError("ARCHIVE_MANIFEST_BUDGET")
                marker = data.get("next_marker", "")
                if not marker:
                    break
            else:
                raise AdapterError("ARCHIVE_MANIFEST_PAGE_LIMIT")
        return result

    async def output_manifest(self, cid):
        queue, result = [(cid, "")], []
        while queue:
            parent, path = queue.pop(0)
            offset = 0
            for _ in range(100):
                page = await self.files(parent, offset)
                for file in page.files:
                    relative = str(PurePosixPath(path) / file.name)
                    if file.is_dir:
                        queue.append((file.fid, relative))
                    else:
                        result.append({"path": relative, "size": file.size,
                                       "fid": file.fid, "cid": parent, "sha1": file.sha1})
                    if len(result) + len(queue) > 10000:
                        raise AdapterError("OUTPUT_MANIFEST_BUDGET")
                if page.next_offset is None:
                    break
                offset = page.next_offset
            else:
                raise AdapterError("OUTPUT_MANIFEST_PAGE_LIMIT")
        return result

    async def close(self):
        close = getattr(self.client, "close", None)
        if close:
            close()

    async def download(self, pickcode):
        if self.governor:
            try:
                self.governor.before()
            except ValueError:
                raise AdapterError("REMOTE_CIRCUIT_OPEN") from None
        async with self.request_lock:
            if self.governor:
                try:
                    self.governor.before()
                except ValueError:
                    raise AdapterError("REMOTE_CIRCUIT_OPEN") from None
            await asyncio.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 1
            try:
                self.metrics["requests"] += 1
                value = await asyncio.wait_for(self.client.download_url(
                    pickcode, user_agent="115work", async_=True,
                ), self.timeout)
                return {"url": str(value), "headers": value.get("headers", {}),
                        "sha1": value.get("sha1", "")}
            except Exception:
                self.metrics["errors"] += 1
                if self.governor:
                    self.governor.failure("DOWNLOAD_CAPABILITY_UNAVAILABLE")
                raise AdapterError("DOWNLOAD_CAPABILITY_UNAVAILABLE") from None

    async def verify_locator(self, locator):
        offset = 0
        for _ in range(100):
            page = await self.files(locator["cid"], offset)
            matching = [file for file in page.files if file.fid == locator["fid"]]
            if matching:
                file = matching[0]
                if file.size != locator["size"] or (locator.get("sha1") and
                                                    file.sha1 != locator["sha1"]):
                    raise AdapterError("SOURCE_VERSION_NOT_VERIFIED")
                return file
            if page.next_offset is None:
                break
            offset = page.next_offset
        raise AdapterError("SOURCE_VERSION_NOT_VERIFIED")

    async def move_verified(self, locator, cid):
        await self.verify_locator(locator)
        await self.call("fs_move", locator["fid"], pid=cid, mutation=True)

    async def delete_verified(self, locator):
        await self.verify_locator(locator)
        await self.call("fs_delete", locator["fid"], mutation=True)


class FakeP115Adapter:
    """Explicit development/test mode, never selected implicitly."""

    def __init__(self):
        self.submissions: list = []
        self.statuses: list[OfflineStatus] = []
        self.folders: dict = {}
        self.listings: dict[str, list[CloudFile]] = {}
        self.snapshot_calls = 0
        self.file_calls: list[str] = []
        self.extract_results: list[str] = ["success"]

    async def folder(self, parent: str, name: str) -> str:
        return self.folders.setdefault((parent, name), f"folder-{len(self.folders) + 1}")

    async def submit(self, links: list[str], cid: str):
        self.submissions.append((list(links), cid))

    async def offline_snapshot(self) -> list[OfflineStatus]:
        self.snapshot_calls += 1
        return self.statuses

    async def files(self, cid: str, offset: int = 0) -> FilePage:
        self.file_calls.append(cid)
        files = self.listings.get(cid, [])
        return FilePage(files[offset:offset + 115], offset + 115 if offset + 115 < len(files) else None)

    async def prepare_extract(self, pickcode: str, password: str = "") -> str:
        return "success"

    async def inspect_extract(self, pickcode: str) -> str:
        return "success"

    async def extract(self, pickcode: str, output_cid: str) -> str:
        self.listings[output_cid] = [CloudFile("result", output_cid, "result.txt", 1)]
        return "fake-" + pickcode

    async def extract_status(self, remote_id: str) -> str:
        return self.extract_results.pop(0) if len(self.extract_results) > 1 else self.extract_results[0]

    async def archive_manifest(self, pickcode):
        return [{"path": "result.txt", "size": 1}]

    async def output_manifest(self, cid):
        return [{"path": f.name, "size": f.size} for f in self.listings.get(cid, []) if not f.is_dir]
