"""Shared investigation run states and validated transitions."""

RUN_ACTIVE_STATUSES = ("queued", "running", "waiting_for_review")
RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
RUN_STATUSES = frozenset({*RUN_ACTIVE_STATUSES, *RUN_TERMINAL_STATUSES})

RUN_TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset({"queued", "waiting_for_review", "failed", "cancelled"}),
    "waiting_for_review": frozenset({"completed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


def status_check_sql(column: str = "status") -> str:
    values = ",".join("'" + value.replace("'", "''") + "'" for value in sorted(RUN_STATUSES))
    return f"{column} in ({values})"


class InvalidRunTransition(ValueError):
    pass


def transition_run(run, target: str) -> bool:
    """Set a valid status. Repeating the current status is an idempotent no-op."""
    current = run.status
    if target not in RUN_STATUSES:
        raise InvalidRunTransition(f"Unknown investigation status: {target}")
    if current == target:
        return False
    if target not in RUN_TRANSITIONS.get(current, ()):
        raise InvalidRunTransition(f"Invalid investigation transition: {current} -> {target}")
    run.status = target
    return True
