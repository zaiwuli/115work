import time

from sqlalchemy import select, update

from app.persistence.models import Job


def enqueue(session, key: str, kind: str, payload: dict, due: float | None = None) -> Job:
    due = time.time() if due is None else due
    existing = session.scalar(select(Job).where(Job.key == key))
    if existing:
        if kind == "cloud_scan":
            existing.payload = existing.payload | {
                "offline_only": existing.payload.get("offline_only", False) and payload.get("offline_only", False),
            }
        if existing.state in {"RUNNING", "LEASED"}:
            existing.dirty = True
        elif existing.state not in {"RECONCILING"}:
            old_state = existing.state
            existing.state = "READY"
            existing.due = min(existing.due, due) if old_state == "READY" else due
            existing.payload = payload | ({"offline_only": existing.payload["offline_only"]}
                                          if kind == "cloud_scan" else {})
        return existing
    job = Job(key=key, kind=kind, payload=payload, due=due)
    session.add(job)
    return job


class JobStore:
    def __init__(self, db, owner: str):
        self.db, self.owner = db, owner

    def claim(self, now: float, kinds: list[str] | None = None, restricted_ids=None) -> Job | None:
        with self.db.session() as session:
            query = select(Job).where(Job.state == "READY", Job.due <= now).order_by(Job.due)
            if kinds is not None:
                query = query.where(Job.kind.in_(kinds))
            if restricted_ids is not None:
                query = query.where(Job.id.in_(restricted_ids) | Job.kind.in_(["notifications", "cache_gc"]))
            candidate = session.scalar(query.limit(1))
            if not candidate:
                return None
            changed = session.execute(
                update(Job).where(Job.id == candidate.id, Job.state == "READY").values(
                    state="RUNNING", owner=self.owner, generation=Job.generation + 1,
                    lease_until=now + 90, dispatch_count=Job.dispatch_count + 1,
                )
            )
            if changed.rowcount != 1:
                return None
            session.refresh(candidate)
            return candidate

    def owned(self, session, job: Job) -> Job:
        current = session.get(Job, job.id)
        if (
            not current or current.owner != self.owner or current.generation != job.generation
            or current.state != "RUNNING" or current.lease_until < time.time()
        ):
            raise RuntimeError("STALE_EXECUTION")
        return current

    def heartbeat(self, job: Job) -> bool:
        with self.db.session() as session:
            changed = session.execute(update(Job).where(
                Job.id == job.id, Job.owner == self.owner, Job.generation == job.generation,
                Job.state == "RUNNING", Job.lease_until > time.time(),
            ).values(lease_until=time.time() + 90))
            return changed.rowcount == 1

    def finish(self, job: Job, *, due: float | None = None, error: str = ""):
        with self.db.session() as session:
            current = self.owned(session, job)
            if current.dirty or due is not None:
                current.state, current.due = "READY", due if due is not None else time.time()
            else:
                current.state = "SUCCEEDED"
            current.dirty, current.owner, current.lease_until = False, "", None
            current.error = error

    def recover(self):
        with self.db.session() as session:
            # Local file lock proves no other owner can still execute on this data directory.
            for job in session.scalars(select(Job).where(Job.state.in_(["RUNNING", "LEASED"]))):
                job.state, job.owner, job.lease_until = "RECONCILING", "", None
