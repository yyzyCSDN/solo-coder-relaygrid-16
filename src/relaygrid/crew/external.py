from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from .routing import RoadEdge, shortest_minutes
from .shifts import Assignment, Shift, rest_violation, worked_minutes


class ArrivalStatus(str, Enum):
    EXPECTED = "expected"
    ENROUTE = "enroute"
    ARRIVED = "arrived"


@dataclass(frozen=True)
class Certificate:
    skill: str
    issued_min: float
    expires_min: float = float("inf")

    def valid_at(self, at_min: float) -> bool:
        return self.issued_min <= at_min < self.expires_min


@dataclass
class ExternalCrew:
    crew_id: str
    unit: str
    certificates: dict[str, Certificate]
    arrival: ArrivalStatus
    station: str
    shift: Shift
    assignments: list[Assignment] = field(default_factory=list)

    def certified(self, skill: str, at_min: float) -> bool:
        cert = self.certificates.get(skill)
        return cert is not None and cert.valid_at(at_min)


@dataclass(frozen=True)
class CrewBasis:
    # Immutable snapshot stored on each dispatch so historical decisions
    # stay auditable after certificates or shifts change.
    crew_id: str
    unit: str
    station: str
    arrival: ArrivalStatus
    shift: Shift
    certificates: tuple[Certificate, ...]


@dataclass(frozen=True)
class AidJob:
    job_id: str
    node: str
    skill: str
    start_min: float
    duration_min: float


class AidRoster:
    def __init__(self, edges: list[RoadEdge] | None = None):
        self.crews: dict[str, ExternalCrew] = {}
        self.edges: list[RoadEdge] = list(edges or [])

    def register(self, crew: ExternalCrew) -> None:
        if crew.crew_id in self.crews:
            raise ValueError(f"crew {crew.crew_id} already registered")
        if crew.shift.crew_id != crew.crew_id:
            raise ValueError("shift crew_id mismatch")
        self.crews[crew.crew_id] = crew

    def set_arrival(self, crew_id: str, status: ArrivalStatus) -> None:
        self.crews[crew_id].arrival = status

    def update_shift(self, crew_id: str, shift: Shift) -> None:
        if shift.crew_id != crew_id:
            raise ValueError("shift crew_id mismatch")
        self.crews[crew_id].shift = shift

    def replace_certificate(self, crew_id: str, cert: Certificate) -> None:
        self.crews[crew_id].certificates[cert.skill] = cert

    def revoke_certificate(self, crew_id: str, skill: str) -> None:
        self.crews[crew_id].certificates.pop(skill, None)

    def basis_for(self, crew_id: str) -> CrewBasis:
        crew = self.crews[crew_id]
        return CrewBasis(
            crew_id=crew.crew_id,
            unit=crew.unit,
            station=crew.station,
            arrival=crew.arrival,
            shift=crew.shift,
            certificates=tuple(crew.certificates.values()),
        )


def crew_eligible(
    crew: ExternalCrew,
    job: AidJob,
    edges: list[RoadEdge],
    *,
    at_min: float | None = None,
    check_deadline: bool = True,
) -> tuple[bool, list[str], float | None]:
    # Qualification, road access, work hours, and handover/rest limits are
    # all enforced together; every failed rule is reported back.
    reasons: list[str] = []
    end = job.start_min + job.duration_min

    if crew.arrival is not ArrivalStatus.ARRIVED:
        reasons.append("arrival")
    if not crew.certified(job.skill, job.start_min):
        reasons.append("certificate")

    travel = shortest_minutes(crew.station, job.node, edges)
    if travel == float("inf"):
        reasons.append("road")
        travel = None
    elif check_deadline and at_min is not None and at_min + travel > job.start_min:
        reasons.append("deadline")

    shift = crew.shift
    if job.start_min < shift.start_min or end > shift.end_min:
        reasons.append("shift_window")

    candidate = Assignment(crew.crew_id, job.job_id, job.start_min, end)
    for a in crew.assignments:
        if a.crew_id != crew.crew_id:
            continue
        overlap = not (end <= a.start_min or job.start_min >= a.end_min)
        if overlap:
            reasons.append("handover")
        elif a.end_min <= job.start_min:
            if rest_violation(a, candidate, shift.min_rest_min):
                reasons.append("rest")
        elif end <= a.start_min:
            if rest_violation(candidate, a, shift.min_rest_min):
                reasons.append("rest")

    projected = (
        worked_minutes(crew.assignments, crew.crew_id, shift.start_min, shift.end_min)
        + job.duration_min
    )
    if projected > shift.max_work_min:
        reasons.append("work_hours")

    return not reasons, sorted(set(reasons)), travel


def select_aid_crew(
    roster: AidRoster, job: AidJob, at_min: float | None = None
) -> tuple[dict | None, dict[str, list[str]]]:
    ranked: list[tuple[float, str]] = []
    rejections: dict[str, list[str]] = {}
    for crew in roster.crews.values():
        ok, reasons, travel = crew_eligible(
            crew, job, roster.edges, at_min=at_min
        )
        if not ok:
            rejections[crew.crew_id] = reasons
            continue
        ranked.append((travel, crew.crew_id))
    if not ranked:
        return None, rejections
    travel, crew_id = min(ranked)
    eta = at_min + travel if at_min is not None else travel
    return {"crew_id": crew_id, "eta_min": eta, "travel_min": travel}, rejections


class DispatchState(str, Enum):
    PENDING = "pending"
    STARTED = "started"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class AidDispatch:
    dispatch_id: int
    job: AidJob
    crew_id: str
    basis: CrewBasis
    state: DispatchState = DispatchState.PENDING

    @property
    def started(self) -> bool:
        return self.state in (DispatchState.STARTED, DispatchState.COMPLETED)


class AidDispatchLedger:
    def __init__(self, roster: AidRoster):
        self.roster = roster
        self._records: dict[int, AidDispatch] = {}
        self._next_id = 1

    @property
    def records(self) -> list[AidDispatch]:
        return [self._records[k] for k in sorted(self._records)]

    def plan(self, job: AidJob, at_min: float | None = None) -> AidDispatch | None:
        choice, _ = select_aid_crew(self.roster, job, at_min)
        if choice is None:
            return None
        crew_id = choice["crew_id"]
        record = AidDispatch(
            self._next_id, job, crew_id, self.roster.basis_for(crew_id)
        )
        self._records[self._next_id] = record
        self._next_id += 1
        crew = self.roster.crews[crew_id]
        crew.assignments.append(
            Assignment(
                crew_id,
                job.job_id,
                job.start_min,
                job.start_min + job.duration_min,
            )
        )
        return record

    def mark_started(self, dispatch_id: int) -> None:
        record = self._records[dispatch_id]
        if record.state is not DispatchState.PENDING:
            raise ValueError("dispatch is not pending")
        record.state = DispatchState.STARTED

    def mark_completed(self, dispatch_id: int) -> None:
        record = self._records[dispatch_id]
        if record.state is not DispatchState.STARTED:
            raise ValueError("dispatch is not started")
        record.state = DispatchState.COMPLETED

    def cancel(self, dispatch_id: int) -> None:
        record = self._records[dispatch_id]
        if record.state is not DispatchState.PENDING:
            raise ValueError("only pending dispatches can be cancelled")
        record.state = DispatchState.CANCELLED
        crew = self.roster.crews[record.crew_id]
        crew.assignments = [
            a for a in crew.assignments if a.job_id != record.job.job_id
        ]

    def revalidate_pending(
        self, at_min: float | None = None
    ) -> dict[int, list[str]]:
        # Certificate expiry and shift changes only reach not-started
        # dispatches; started/completed records keep their stored basis.
        failures: dict[int, list[str]] = {}
        for record in self._records.values():
            if record.state is not DispatchState.PENDING:
                continue
            crew = self.roster.crews.get(record.crew_id)
            if crew is None:
                failures[record.dispatch_id] = ["removed"]
                continue
            view = replace(
                crew,
                assignments=[
                    a for a in crew.assignments if a.job_id != record.job.job_id
                ],
            )
            _, reasons, _ = crew_eligible(
                view,
                record.job,
                self.roster.edges,
                at_min=at_min,
                check_deadline=False,
            )
            if reasons:
                failures[record.dispatch_id] = reasons
        return failures
