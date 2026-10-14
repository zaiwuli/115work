from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORK115_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    version: str = "0.1.0"
    build_revision: str = "local"
    build_time: str = ""
    local_root: Path | None = None
    api_token: SecretStr = SecretStr("")
    local_no_auth: bool = True
    cookie: SecretStr = SecretStr("")
    adapter: str = "disabled"
    allowed_source_roots: list[Path] = []
    allowed_output_roots: list[Path] = []
    allowed_webhook_hosts: list[str] = []
    scheduler_enabled: bool = True
    tick_seconds: float = Field(default=5, ge=0.1)
    stable_seconds: float = Field(default=15, ge=0)
    max_document_bytes: int = 4 * 1024 * 1024
    max_extract_bytes: int = 20 * 1024**3
    max_extract_files: int = 100000
    remote_timeout: float = 45
    extractor_path: str = ""
    xtractr_path: str = ""
    extract_engine: Literal["xtractr", "7zip"] = "xtractr"
    extract_timeout: int = 1800
    cache_max_bytes: int = 100 * 1024**3
    allowed_download_hosts: list[str] = [".115.com", ".115cdn.com", ".115cdn.net", ".anxia.com"]
    allowed_local_download_hosts: list[str] = []
    watcher_enabled: bool = True
    document_timeout: int = 30
    delete_delay_seconds: int = 86400
    discovery_timeout: int = 86400
    cloud_observation_timeout: int = 86400

    @property
    def db_path(self) -> Path:
        return self.data_dir / "config" / "115work.db"

    def local_path(self, legacy: str) -> Path:
        if self.local_root is None:
            return self.data_dir / legacy
        names = {
            "watch": "监控目录", "output": "解压目录", "cache": "缓存目录",
            "archive": "归档目录", "inbox/links": "离线磁链",
            "inbox/processed": "离线磁链/已处理", "inbox/failed": "离线磁链/无效文件",
            "inbox/partial": "离线磁链/部分失败",
        }
        return self.local_root / names[legacy]

    def prepare(self) -> None:
        self.data_dir = self.data_dir.resolve()
        if self.data_dir == Path(self.data_dir.anchor):
            raise ValueError("DATA_DIR_CANNOT_BE_FILESYSTEM_ROOT")
        if self.local_root is not None:
            self.local_root = self.local_root.resolve()
            if not self.local_root.is_dir() or self.local_root == Path(self.local_root.anchor):
                raise ValueError("LOCAL_ROOT_MUST_BE_EXISTING_MOUNT")
        for directory in (
            "config", "watch", "inbox/links", "inbox/processed", "inbox/failed",
            "output", "cache", "archive",
        ):
            (self.data_dir / directory).mkdir(parents=True, exist_ok=True)
        for directory in ("watch", "output", "cache", "archive", "inbox/links",
                          "inbox/processed", "inbox/failed", "inbox/partial"):
            self.local_path(directory).mkdir(parents=True, exist_ok=True)
