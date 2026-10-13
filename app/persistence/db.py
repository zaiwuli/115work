from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.persistence.models import Settings


DEFAULT_SETTINGS = {
    "paused": False,
    "local_extract_concurrency": 1,
    "cloud_root_cid": "",
    "cloud_archive_cid": "",
    "cloud_failed_cid": "",
    "life_monitor_enabled": False,
    "life_monitor_started": 0,
    "offline_root_cid": "0",
    "cloud_output_cid": "0",
    "offline_extract_cid": "",
    "cloud_extract_by_date": True,
    "cloud_retry_seconds": 120,
    "cloud_cooldown_seconds": 30,
    "offline_fallback_seconds": 3600,
    "notification_offline": True,
    "notification_cloud": True,
    "notification_local": True,
    "notification_webhook": "",
    "notification_enabled": True,
    "notification_provider": "ms",
    "notification_template": "",
    "notification_discovery": True,
    "notification_cache": True,
    "notification_extract": True,
    "notification_complete": True,
    "notification_cleanup": True,
    "passwords": [],
    "mappings": [],
}


class Database:
    def __init__(self, path: Path):
        self.engine = create_engine(
            "sqlite:///" + path.as_posix(),
            connect_args={"check_same_thread": False, "timeout": 10},
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=10000")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions() as session:
            with session.begin():
                yield session

    def settings(self) -> dict:
        with self.session() as session:
            saved = {x.key: x.value["value"] for x in session.scalars(select(Settings))
                     if "value" in x.value}
        return DEFAULT_SETTINGS | saved

    def set_settings(self, values: dict) -> None:
        with self.session() as session:
            for key, value in values.items():
                session.merge(Settings(key=key, value={"value": value}))
