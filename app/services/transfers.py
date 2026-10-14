import asyncio
import hashlib
import ipaddress
import json
import re
import shutil
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from app.persistence.models import CacheEntry, Task, Transfer
from app.adapters.safe_http import SafeHTTPTransport
from app.domain.paths import safe_relative
from app.services.events import transition


def host_allowed(host, patterns):
    return any(host == p or (p.startswith(".") and host.endswith(p) and host != p[1:])
               for p in patterns)


class TransferService:
    def __init__(self, db, config, store, adapter):
        self.db, self.config, self.store, self.adapter = (
            db, config, store, adapter,
        )

    async def validate_url(self, url):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        trusted_local = host in self.config.allowed_local_download_hosts
        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("UNSAFE_DOWNLOAD_URL")
        if not trusted_local and not host_allowed(host, self.config.allowed_download_hosts):
            raise ValueError("DOWNLOAD_HOST_NOT_AUTHORIZED")
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (
            443 if parsed.scheme == "https" else 80
        ), type=socket.SOCK_STREAM)
        if not trusted_local and any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("DOWNLOAD_PRIVATE_ADDRESS_REJECTED")
        return host, list(dict.fromkeys(a[4][0] for a in addresses))

    async def download(self, task, job):
        try:
            if task.locator.get("remote_parts"):
                main = None
                for part in task.locator["remote_parts"]:
                    member = SimpleNamespace(
                        id=task.id, source=task.source, name=part["name"], size=part["size"],
                        object_key=task.object_key,
                        locator=part | {"connection_id": task.locator["connection_id"]},
                        group_version={"object_key": task.object_key, "locator": task.locator,
                                       "size": task.size},
                    )
                    path = await self._download(member, job)
                    if path is None:
                        return None
                    if part["path"] == task.locator["path"]:
                        main = path
                with self.db.session() as session:
                    session.get(Transfer, task.id).state = "SUCCEEDED"
                return main
            return await self._download(task, job)
        except Exception as exc:
            with self.db.session() as session:
                self.store.owned(session, job)
                saved = session.get(Transfer, task.id)
                if saved:
                    saved.attempts += 1
                    saved.error = str(exc) if isinstance(exc, ValueError) else "DOWNLOAD_FAILED"
            raise

    async def _download(self, task, job):
        cache = self.config.local_path("cache") / task.id
        cache.mkdir(exist_ok=True)
        name = Path(task.name).name
        if name != task.name or name in {"", ".", ".."} or len(safe_relative(name).parts) != 1:
            raise ValueError("UNSAFE_CACHE_NAME")
        target, part, marker = cache / name, cache / (name + ".part"), cache / (name + ".COMPLETE.json")
        version = getattr(task, "group_version", {
            "object_key": task.object_key, "locator": task.locator, "size": task.size,
        })
        if target.exists():
            if not marker.exists():
                raise ValueError("CACHE_COMPLETION_EVIDENCE_REQUIRED")
            saved = json.loads(marker.read_text())
            from app.services.retention import digest
            if saved["version"] != version or await asyncio.to_thread(digest, target) != saved["sha256"]:
                raise ValueError("CACHE_VERSION_OR_HASH_MISMATCH")
            return target
        if self.db.settings().get("downloads_paused") or self.db.settings()["paused"]:
            return None
        if task.source == "115":
            route = await self.adapter.download(task.locator["pickcode"])
        else:
            raise ValueError("UNSUPPORTED_DOWNLOAD_SOURCE")
        parsed_route = urlsplit(route["url"])
        with self.db.session() as session:
            self.store.owned(session, job)
            current = session.get(Task, task.id)
            current.output = current.output | {"download_endpoint": {
                "scheme": parsed_route.scheme, "host": parsed_route.hostname or "",
            }}
        host, addresses = await self.validate_url(route["url"])
        with self.db.session() as session:
            self.store.owned(session, job)
            saved = session.get(Transfer, task.id)
            if saved is None:
                saved = Transfer(task_id=task.id, expected_size=version["size"], version=version,
                                 attempts=0)
                session.add(saved)
            if saved.version != version:
                raise ValueError("TRANSFER_VERSION_CHANGED")
            if saved.attempts >= 5:
                raise ValueError("TRANSFER_RETRY_EXHAUSTED")
            current_task = session.get(Task, task.id)
            saved.state, saved.error = "RUNNING", ""
            transition(session, current_task, "RUNNING",
                       "document_copy" if current_task.stage == "document_copy" else "cache")
        offset = part.stat().st_size if part.exists() else 0
        used = await asyncio.to_thread(self.cache_bytes)
        if offset > task.size or used + task.size - offset > self.config.cache_max_bytes:
            raise ValueError("CACHE_CAPACITY_LIMIT")
        if shutil.disk_usage(cache).free < task.size - offset + 64 * 1024**2:
            raise ValueError("INSUFFICIENT_CACHE_SPACE")
        if part.exists() and offset == task.size:
            return await self.publish(task, job, part, target, marker, version, route)
        headers = dict(route.get("headers", {}))
        headers["Accept-Encoding"] = "identity"
        if offset:
            headers["Range"] = f"bytes={offset}-"
        start, checkpoint, initial = time.monotonic(), time.monotonic(), offset
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15),
                                     follow_redirects=False, trust_env=False,
                                     transport=SafeHTTPTransport(host, addresses)) as client:
            async with client.stream("GET", route["url"], headers=headers) as response:
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("ENCODED_RANGE_TRANSFER_REJECTED")
                if offset:
                    expected_range = re.match(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                    if response.status_code != 206 or not expected_range or (
                        int(expected_range[1]) != offset or int(expected_range[3]) != task.size
                        or int(expected_range[2]) != task.size - 1
                    ):
                        raise ValueError("RANGE_RESUME_NOT_CONFIRMED")
                elif response.status_code != 200:
                    raise ValueError("DOWNLOAD_REQUEST_REJECTED")
                response.raise_for_status()
                with part.open("ab") as writer:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if self.db.settings().get("downloads_paused") or self.db.settings()["paused"]:
                            return None
                        with self.db.session() as session:
                            self.store.owned(session, job)
                            if session.get(Task, task.id).cancel_requested:
                                return None
                        offset += len(chunk)
                        if offset > task.size:
                            raise ValueError("DOWNLOAD_SIZE_LIMIT")
                        await asyncio.to_thread(writer.write, chunk)
                        if time.monotonic() - checkpoint >= 1:
                            with self.db.session() as session:
                                transfer = session.get(Transfer, task.id)
                                transfer.bytes_done = offset
                                transfer.speed = (offset - initial) / max(0.001, time.monotonic() - start)
                                transfer.eta = (task.size - offset) / max(1, transfer.speed)
                                transfer.updated_at = time.time()
                            checkpoint = time.monotonic()
        if offset != task.size:
            raise ValueError("CACHE_INCOMPLETE")
        return await self.publish(task, job, part, target, marker, version, route)

    async def publish(self, task, job, part, target, marker, version, route):
        from app.services.retention import digest
        sha256 = await asyncio.to_thread(digest, part)
        if route.get("sha1"):
            def sha1():
                hasher = hashlib.sha1()
                with part.open("rb") as reader:
                    while chunk := reader.read(1024 * 1024):
                        hasher.update(chunk)
                return hasher.hexdigest()
            if (await asyncio.to_thread(sha1)).lower() != route["sha1"].lower():
                raise ValueError("PROVIDER_HASH_MISMATCH")
        with self.db.session() as session:
            self.store.owned(session, job)
            if session.get(Task, task.id).cancel_requested:
                return None
            evidence = {"version": version, "sha256": sha256}
            marker.write_text(json.dumps(evidence), encoding="utf-8")
            part.rename(target)
            session.merge(CacheEntry(task_id=task.id, path=str(target.parent), size=version["size"],
                                     verification="provider_hash" if route.get("sha1") else "size_sha256",
                                     manifest=evidence))
            transfer = session.get(Transfer, task.id)
            transfer.bytes_done, transfer.state = task.size, "SUCCEEDED"
        return target

    def cache_bytes(self):
        root = self.config.local_path("cache")
        return sum(path.stat().st_size for path in root.rglob("*")
                   if path.is_file() and not path.is_symlink())
