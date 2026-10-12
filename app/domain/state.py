from enum import StrEnum


class State(StrEnum):
    READY = "READY"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    WAITING_RETRY = "WAITING_RETRY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    PAUSED = "PAUSED"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    SKIPPED = "SKIPPED"


TERMINAL = {State.SUCCEEDED, State.FAILED, State.CANCELED, State.SKIPPED}
ACTIVE = set(State) - TERMINAL


def validate_transition(old: str, new: str, *, manual_retry: bool = False) -> None:
    if old == new:
        return
    if old in TERMINAL:
        if old == State.FAILED and new == State.READY and manual_retry:
            return
        raise ValueError("TERMINAL_TRANSITION_REQUIRES_EXPLICIT_REPROCESS")
    if new not in State:
        raise ValueError("UNKNOWN_STATE")
