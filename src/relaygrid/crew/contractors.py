"""External contractor crew roster and dispatch selection.

A contractor crew (外援班组) records the unit it belongs to, the skill
certifications it holds (with expiry), its arrival status at the local
base, its shift window, and its minimum rest between assignments.

Dispatch selection must satisfy four constraints at the planned start:
  * qualification: a certification for the required skill that is valid
    at the task start time;
  * road: an unblocked road path from the crew base to the job node;
  * work duration: the task fits inside the shift and within the maximum
    work minutes;
  * handover: the minimum rest after the previous assignment before the
    next one, with no overlapping tasks.

Certificate expiry or shift changes only affect tasks that have not
started; historical dispatch records keep the basis they were approved
under and remain auditable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .routing import RoadEdge, shortest_minutes
from .shifts import Assignment, worked_minutes


class ArrivalStatus(str, Enum):
    ARRIVED = "arrived"
    EN_ROUTE = "en_route"
    PENDING = "pending"


@dataclass(frozen=True)
class Certification:
    """Skill certification with an expiry time (inf = never expires)."""

    skill: str
    expires_min: float = float("inf")


@dataclass(frozen=True)
class ContractorCrew:
    """Roster entry for an external contractor crew."""

    crew_id: str
    unit: str
    base_node: str
    shift_start_min: float
    shift_end_min: float
    max_work_min: float
    min_rest_min: float
    certifications: tuple[Certification, ...] = ()
    arrival_status: ArrivalStatus = ArrivalStatus.PENDING
    arrival_min: float = 0.0

    def certified(self, skill: str, at_min: float) -> bool:
        return any(
            c.skill == skill and c.expires_min >= at_min
            for c in self.certifications
        )

    def cert_expiry(self, skill: str) -> float | None:
        """Earliest expiry of the certifications for a skill, if any."""
        exp = [c.expires_min for c in self.certifications if c.skill == skill]
        return min(exp) if exp else None

    def available_from(self) -> float:
        return max(self.shift_start_min, self.arrival_min)


@dataclass(frozen=True)
class DispatchBasis:
    """Snapshot of the constraints a dispatch was approved under."""

    skill: str
    cert_expires_min: float | None
    travel_min: float
    eta_min: float
    shift_start_min: float
    shift_end_min: float
    max_work_min: float
    min_rest_min: float


@dataclass(frozen=True)
class DispatchRecord:
    """Auditable record of a dispatched task.

    The basis is frozen at approval time so later certificate expiry or
    shift changes cannot rewrite historical dispatches.
    """

    record_id: str
    crew_id: str
    unit: str
    job_id: str
    job_node: str
    start_min: float
    end_min: float
    basis: DispatchBasis


def _last_assignment_end(assignments: list[Assignment], crew_id: str) -> float | None:
    ends = [a.end_min for a in assignments if a.crew_id == crew_id]
    return max(ends) if ends else None


def rest_ok(crew: ContractorCrew, assignments: list[Assignment], start_min: float) -> bool:
    """Handover constraint: minimum rest after the previous assignment."""
    last_end = _last_assignment_end(assignments, crew.crew_id)
    if last_end is None:
        return True
    return start_min - last_end >= crew.min_rest_min


def overlaps(crew: ContractorCrew, assignments: list[Assignment], start_min: float, end_min: float) -> bool:
    return any(
        a.crew_id == crew.crew_id
        and not (end_min <= a.start_min or start_min >= a.end_min)
        for a in assignments
    )


def select_contractor(
    crews: list[ContractorCrew],
    job_node: str,
    skill: str,
    start_min: float,
    duration: float,
    edges: list[RoadEdge],
    assignments: list[Assignment],
    *,
    require_arrived: bool = False,
):
    """Pick the crew with the earliest ETA satisfying all four constraints.

    Returns ``(crew, travel_min, eta_min)`` or ``None`` when no crew
    qualifies.
    """
    chosen = None
    for crew in crews:
        if require_arrived and crew.arrival_status != ArrivalStatus.ARRIVED:
            continue
        # qualification: certificate for the skill valid at task start
        if not crew.certified(skill, start_min):
            continue
        # road: unblocked path from the crew base to the job node
        travel = shortest_minutes(crew.base_node, job_node, edges)
        if travel == float("inf"):
            continue
        earliest = max(start_min, crew.available_from())
        end = earliest + duration
        # work duration: task inside the shift and within max work minutes
        if end > crew.shift_end_min:
            continue
        if (
            worked_minutes(
                assignments, crew.crew_id, crew.shift_start_min, crew.shift_end_min
            )
            + duration
            > crew.max_work_min
        ):
            continue
        # handover: minimum rest after the previous assignment, no overlap
        if not rest_ok(crew, assignments, earliest):
            continue
        if overlaps(crew, assignments, earliest, end):
            continue
        eta = earliest + travel
        if chosen is None or eta < chosen[2]:
            chosen = (crew, travel, eta)
    return chosen


def dispatch_contractor(
    record_id: str,
    job_id: str,
    crews: list[ContractorCrew],
    job_node: str,
    skill: str,
    start_min: float,
    duration: float,
    edges: list[RoadEdge],
    assignments: list[Assignment],
    *,
    require_arrived: bool = False,
) -> DispatchRecord | None:
    """Select a crew and record the dispatch basis for audit.

    Returns the :class:`DispatchRecord`, or ``None`` when no crew
    qualifies.
    """
    picked = select_contractor(
        crews,
        job_node,
        skill,
        start_min,
        duration,
        edges,
        assignments,
        require_arrived=require_arrived,
    )
    if picked is None:
        return None
    crew, travel, eta = picked
    basis = DispatchBasis(
        skill=skill,
        cert_expires_min=crew.cert_expiry(skill),
        travel_min=travel,
        eta_min=eta,
        shift_start_min=crew.shift_start_min,
        shift_end_min=crew.shift_end_min,
        max_work_min=crew.max_work_min,
        min_rest_min=crew.min_rest_min,
    )
    return DispatchRecord(
        record_id,
        crew.crew_id,
        crew.unit,
        job_id,
        job_node,
        eta,
        eta + duration,
        basis,
    )


def revalidate(
    records: list[DispatchRecord],
    crews: list[ContractorCrew],
    edges: list[RoadEdge],
    now_min: float,
):
    """Re-check unstarted records against the current roster and road network.

    Records starting at or before ``now_min`` are historical: they keep the
    basis they were approved under and are returned unchanged. Unstarted
    records are re-checked against the current certificates, shift windows,
    road network, and rest state; records that no longer qualify are
    returned with a reason.

    Returns ``(historical, still_valid, invalid)`` where ``invalid`` is a
    list of ``(record, reason)`` pairs.
    """
    by_crew = {c.crew_id: c for c in crews}
    historical: list[DispatchRecord] = []
    valid: list[DispatchRecord] = []
    invalid: list[tuple[DispatchRecord, str]] = []
    for rec in records:
        if rec.start_min <= now_min:
            historical.append(rec)
            continue
        crew = by_crew.get(rec.crew_id)
        reason = None
        if crew is None:
            reason = "crew removed from roster"
        elif not crew.certified(rec.basis.skill, rec.start_min):
            reason = "certificate expired"
        elif (
            rec.start_min < crew.available_from()
            or rec.end_min > crew.shift_end_min
        ):
            reason = "shift changed"
        elif (
            worked_minutes(
                [
                    Assignment(r.crew_id, r.job_id, r.start_min, r.end_min)
                    for r in records
                    if r is not rec
                ],
                crew.crew_id,
                crew.shift_start_min,
                crew.shift_end_min,
            )
            + (rec.end_min - rec.start_min)
            > crew.max_work_min
        ):
            reason = "work duration"
        elif shortest_minutes(crew.base_node, rec.job_node, edges) == float("inf"):
            reason = "road blocked"
        elif not rest_ok(
            crew,
            [
                Assignment(r.crew_id, r.job_id, r.start_min, r.end_min)
                for r in records
                if r is not rec
            ],
            rec.start_min,
        ):
            reason = "handover rest"
        if reason is None:
            valid.append(rec)
        else:
            invalid.append((rec, reason))
    return historical, valid, invalid


def history_for(
    records: list[DispatchRecord],
    *,
    crew_id: str | None = None,
    unit: str | None = None,
    job_id: str | None = None,
) -> list[DispatchRecord]:
    """Look up historical dispatch records by crew, unit, or job."""
    out = []
    for rec in records:
        if crew_id is not None and rec.crew_id != crew_id:
            continue
        if unit is not None and rec.unit != unit:
            continue
        if job_id is not None and rec.job_id != job_id:
            continue
        out.append(rec)
    return out
