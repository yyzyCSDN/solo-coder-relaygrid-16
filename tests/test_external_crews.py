from __future__ import annotations

from relaygrid.crew.external import (
    AidDispatchLedger,
    AidJob,
    AidRoster,
    ArrivalStatus,
    Certificate,
    DispatchState,
    ExternalCrew,
    crew_eligible,
    select_aid_crew,
)
from relaygrid.crew.routing import RoadEdge
from relaygrid.crew.shifts import Assignment, Shift


EDGES = [
    RoadEdge("base", "junction", 10),
    RoadEdge("junction", "site", 20),
    RoadEdge("junction", "cut", 20, blocked=True),
    RoadEdge("remote", "site", 5),
]


def make_crew(
    crew_id="a1",
    *,
    arrival=ArrivalStatus.ARRIVED,
    station="base",
    expires=1000.0,
    start=0.0,
    end=1000.0,
    max_work=600.0,
    min_rest=60.0,
    unit="alpha-contracting",
    assignments=(),
):
    shift = Shift(crew_id, start, end, max_work, min_rest)
    return ExternalCrew(
        crew_id=crew_id,
        unit=unit,
        certificates={"linework": Certificate("linework", 0, expires)},
        arrival=arrival,
        station=station,
        shift=shift,
        assignments=list(assignments),
    )


def make_job(**kw):
    defaults = dict(
        job_id="j1", node="site", skill="linework", start_min=300.0, duration_min=60.0
    )
    defaults.update(kw)
    return AidJob(**defaults)


def test_certificate_validity_window():
    cert = Certificate("linework", 100, 200)
    assert cert.valid_at(100)
    assert cert.valid_at(199.9)
    assert not cert.valid_at(200)
    assert not cert.valid_at(50)
    assert Certificate("x", 0).valid_at(10**9)


def test_not_arrived_rejected():
    roster = AidRoster(EDGES)
    roster.register(make_crew(arrival=ArrivalStatus.EXPECTED))
    choice, rejections = select_aid_crew(roster, make_job())
    assert choice is None
    assert "arrival" in rejections["a1"]


def test_missing_or_expired_cert_rejected():
    roster = AidRoster(EDGES)
    roster.register(make_crew(expires=250.0))  # expires before job start 300
    choice, rejections = select_aid_crew(roster, make_job())
    assert choice is None
    assert "certificate" in rejections["a1"]

    roster.revoke_certificate("a1", "linework")
    _, rejections = select_aid_crew(roster, make_job())
    assert "certificate" in rejections["a1"]


def test_blocked_road_rejected_but_open_route_works():
    roster = AidRoster(EDGES)
    roster.register(make_crew(station="cut"))
    choice, rejections = select_aid_crew(roster, make_job())
    assert choice is None
    assert "road" in rejections["a1"]

    roster2 = AidRoster(EDGES)
    roster2.register(make_crew(station="base"))
    choice, _ = select_aid_crew(roster2, make_job())
    assert choice == {"crew_id": "a1", "eta_min": 30.0, "travel_min": 30.0}


def test_deadline_constraint():
    roster = AidRoster(EDGES)
    roster.register(make_crew(station="base"))  # 30 min travel
    job = make_job(start_min=300)
    choice, rejections = select_aid_crew(roster, job, at_min=280.0)
    assert choice is None
    assert "deadline" in rejections["a1"]
    # Without a decision clock, no deadline is enforced; eta is travel only.
    choice, _ = select_aid_crew(roster, job)
    assert choice["eta_min"] == 30.0


def test_nearest_arrived_qualified_crew_wins():
    roster = AidRoster(EDGES)
    roster.register(make_crew("far", station="base"))   # 30 min
    roster.register(make_crew("near", station="remote"))  # 5 min
    choice, _ = select_aid_crew(roster, make_job(), at_min=0.0)
    assert choice["crew_id"] == "near"
    assert choice["eta_min"] == 5.0


def test_shift_window_and_work_hours_limits():
    # Job ends at 360; shift closes at 330.
    crew = make_crew(end=330.0)
    ok, reasons, _ = crew_eligible(crew, make_job(), EDGES)
    assert not ok and "shift_window" in reasons

    # Prior work leaves only 30 minutes of capacity for a 60-minute job.
    busy = make_crew(max_work=80.0, assignments=[Assignment("a1", "old", 0, 30)])
    ok, reasons, _ = crew_eligible(busy, make_job(), EDGES)
    assert not ok and "work_hours" in reasons


def test_overlap_is_handover_violation_and_gap_is_rest_violation():
    overlapping = make_crew(assignments=[Assignment("a1", "prev", 280, 310)])
    ok, reasons, _ = crew_eligible(overlapping, make_job(), EDGES)
    assert not ok and "handover" in reasons and "rest" not in reasons

    # Ends 30 min before a 300 start; min rest is 60.
    short_gap = make_crew(assignments=[Assignment("a1", "prev", 240, 270)])
    ok, reasons, _ = crew_eligible(short_gap, make_job(), EDGES)
    assert not ok and "rest" in reasons and "handover" not in reasons

    # Gap of exactly min rest is allowed.
    fine = make_crew(assignments=[Assignment("a1", "prev", 200, 240)])
    ok, reasons, _ = crew_eligible(fine, make_job(), EDGES)
    assert ok and reasons == []


def test_rest_after_job_toward_next_assignment():
    next_up = Assignment("a1", "next", 380, 420)  # 20 min after job ends 360
    crew = make_crew(assignments=[next_up])
    ok, reasons, _ = crew_eligible(crew, make_job(), EDGES)
    assert not ok and "rest" in reasons


def test_duplicate_registration_and_shift_mismatch_rejected():
    roster = AidRoster(EDGES)
    roster.register(make_crew())
    try:
        roster.register(make_crew())
        assert False, "expected ValueError"
    except ValueError:
        pass
    try:
        roster.update_shift("a1", Shift("other", 0, 1000, 600, 60))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_ledger_records_basis_and_pending_revalidation():
    roster = AidRoster(EDGES)
    roster.register(make_crew(expires=500.0))
    ledger = AidDispatchLedger(roster)
    job = make_job(start_min=300)
    record = ledger.plan(job)
    assert record is not None
    assert record.state is DispatchState.PENDING
    assert record.basis.unit == "alpha-contracting"
    assert record.basis.certificates[0].expires_min == 500.0

    # Certificate expires before job start: only pending dispatch is affected.
    roster.replace_certificate("a1", Certificate("linework", 0, 250.0))
    failures = ledger.revalidate_pending()
    assert record.dispatch_id in failures
    assert "certificate" in failures[record.dispatch_id]

    # Shift window change is caught too.
    roster.replace_certificate("a1", Certificate("linework", 0, 500.0))
    roster.update_shift("a1", Shift("a1", 0, 330, 600, 60))
    failures = ledger.revalidate_pending()
    assert "shift_window" in failures[record.dispatch_id]


def test_started_dispatch_keeps_historical_basis():
    roster = AidRoster(EDGES)
    roster.register(make_crew(expires=500.0))
    ledger = AidDispatchLedger(roster)
    record = ledger.plan(make_job())
    ledger.mark_started(record.dispatch_id)

    # Expired cert and a changed shift do not touch an in-progress dispatch.
    roster.replace_certificate("a1", Certificate("linework", 0, 100.0))
    roster.update_shift("a1", Shift("a1", 0, 50, 10, 0))
    assert ledger.revalidate_pending() == {}
    assert record.state is DispatchState.STARTED
    assert record.basis.shift.end_min == 1000.0
    assert record.basis.certificates[0].expires_min == 500.0

    ledger.mark_completed(record.dispatch_id)
    assert record.state is DispatchState.COMPLETED


def test_cancel_releases_crew_capacity():
    roster = AidRoster(EDGES)
    roster.register(make_crew(max_work=90.0))
    ledger = AidDispatchLedger(roster)
    r1 = ledger.plan(make_job(job_id="j1", duration_min=60.0))
    # No room for another 60-minute job while the first is pending.
    assert ledger.plan(make_job(job_id="j2", duration_min=60.0)) is None
    ledger.cancel(r1.dispatch_id)
    assert r1.state is DispatchState.CANCELLED
    # Capacity freed; new plan succeeds.
    r2 = ledger.plan(make_job(job_id="j2", duration_min=60.0))
    assert r2 is not None


def test_only_pending_can_cancel_or_start():
    roster = AidRoster(EDGES)
    roster.register(make_crew())
    ledger = AidDispatchLedger(roster)
    record = ledger.plan(make_job())
    ledger.mark_started(record.dispatch_id)
    for action in (ledger.cancel, ledger.mark_started):
        try:
            action(record.dispatch_id)
            assert False, "expected ValueError"
        except ValueError:
            pass
