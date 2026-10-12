import time

from app.persistence.models import Settings


class RemoteGovernor:
    """Durable circuit state; mutations are never retried by this limiter."""
    def __init__(self, db, key="115-main"):
        self.db, self.key = db, "circuit:" + key

    def before(self):
        with self.db.session() as session:
            state = session.get(Settings, self.key)
            if state and state.value.get("until", 0) > time.time():
                raise ValueError("REMOTE_CIRCUIT_OPEN")

    def failure(self, code):
        with self.db.session() as session:
            state = session.get(Settings, self.key)
            count = state.value.get("count", 0) + 1 if state else 1
            delay = 300 if "AUTH" in code else min(3600, 30 * 2 ** min(count, 7))
            session.merge(Settings(key=self.key, value={"count": count, "until": time.time() + delay,
                                                        "error": code}))

    def success(self):
        with self.db.session() as session:
            session.merge(Settings(key=self.key, value={"count": 0, "until": 0, "error": ""}))
