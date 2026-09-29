
from relaygrid.crew.contractors import (
    ArrivalStatus,
    Certification,
    ContractorCrew,
    DispatchRecord,
    dispatch_contractor,
    history_for,
    revalidate,
    select_contractor,
)
from relaygrid.crew.routing import RoadEdge
from relaygrid.crew.shifts import Assignment


def crew(crew_id="c1", unit="UnitA", base="base", shift=(0, 480), max_work=480,
         min_rest=60, certs=(), status=ArrivalStatus.ARRIVED, arrival=0.0):
    return ContractorCrew(
        crew_id, unit, base, shift[0], shift[1], max_work, min_rest,
        tuple(certs), status, arrival,
    )


def test_roster_records_crew_details():
    c = crew(certs=[Certification("lineman", 1000.0)], status=ArrivalStatus.EN_ROUTE, arrival=30.0)
    assert c.unit == "UnitA"
    assert c.certified("lineman", 500.0)
    assert not c.certified("lineman", 1001.0)
    assert c.available_from() == 30.0


def test_qualification_requires_valid_cert_at_start():
    edges = [RoadEdge("base", "job", 10)]
    good = crew(certs=[Certification("lineman", 1000.0)])
    expired = crew("c2", certs=[Certification("lineman", 100.0)])
    expiring = crew("c3", certs=[Certification("lineman", 150.0)])
    assert select_contractor([good], "job", "lineman", 0, 60, edges, []) is not None
    assert select_contractor([expired], "job", "lineman", 200, 60, edges, []) is None
    assert select_contractor([expiring], "job", "lineman", 200, 60, edges, []) is None
    # a different skill is not covered by the certification
    assert select_contractor([good], "job", "electrician", 0, 60, edges, []) is None


def test_road_constraint_blocks_unreachable_crews():
    edges = [RoadEdge("base", "job", 10), RoadEdge("base2", "job", 5, blocked=True)]
    c1 = crew("c1", base="base", certs=[Certification("lineman")])
    c2 = crew("c2", base="base2", certs=[Certification("lineman")])
    picked = select_contractor([c1, c2], "job", "lineman", 0, 60, edges, [])
    assert picked is not None and picked[0].crew_id == "c1"


def test_work_duration_must_fit_shift_and_max_work():
    edges = [RoadEdge("base", "job", 10)]
    c = crew(shift=(0, 480), max_work=240, certs=[Certification("lineman")])
    # task runs past shift end
    assert select_contractor([c], "job", "lineman", 400, 120, edges, []) is None
    # task within shift but over max work minutes
    assert select_contractor([c], "job", "lineman", 0, 300, edges, []) is None
    # task fits
    assert select_contractor([c], "job", "lineman", 0, 200, edges, []) is not None


def test_handover_rest_and_overlap_constraints():
    edges = [RoadEdge("base", "job", 10)]
    c = crew(min_rest=60, certs=[Certification("lineman")])
    # previous assignment ends at 100, new starts at 120 -> only 20 min rest
    assignments = [Assignment("c1", "j0", 0, 100)]
    assert select_contractor([c], "job", "lineman", 120, 60, edges, assignments) is None
    # 60 min rest satisfied
    assert select_contractor([c], "job", "lineman", 160, 60, edges, assignments) is not None
    # overlapping task rejected
    overlap = [Assignment("c1", "j0", 0, 200)]
    assert select_contractor([c], "job", "lineman", 160, 60, edges, overlap) is None


def test_arrival_status_can_be_required():
    edges = [RoadEdge("base", "job", 10)]
    c = crew(status=ArrivalStatus.EN_ROUTE, arrival=100.0, certs=[Certification("lineman")])
    assert select_contractor([c], "job", "lineman", 0, 60, edges, [], require_arrived=True) is None
    picked = select_contractor([c], "job", "lineman", 0, 60, edges, [])
    assert picked is not None and picked[1] == 10 and picked[2] == 110


def test_selection_picks_earliest_eta():
    edges = [RoadEdge("a", "job", 30), RoadEdge("b", "job", 5)]
    far = crew("c1", base="a", certs=[Certification("lineman")])
    near = crew("c2", base="b", certs=[Certification("lineman")])
    picked = select_contractor([far, near], "job", "lineman", 0, 60, edges, [])
    assert picked[0].crew_id == "c2"


def test_dispatch_records_basis_snapshot():
    edges = [RoadEdge("base", "job", 10)]
    c = crew(certs=[Certification("lineman", 500.0)])
    rec = dispatch_contractor("d1", "j1", [c], "job", "lineman", 0, 60, edges, [])
    assert isinstance(rec, DispatchRecord)
    assert rec.crew_id == "c1" and rec.unit == "UnitA"
    assert rec.start_min == 10 and rec.end_min == 70
    assert rec.basis.cert_expires_min == 500.0
    assert rec.basis.travel_min == 10
    assert rec.basis.min_rest_min == 60


def test_revalidate_keeps_historical_basis_after_cert_expiry():
    edges = [RoadEdge("base", "job", 10)]
    valid = crew(certs=[Certification("lineman", 500.0)])
    # both tasks approved while the certificate is still valid
    old = dispatch_contractor("d1", "j1", [valid], "job", "lineman", 0, 60, edges, [])
    future = dispatch_contractor("d2", "j2", [valid], "job", "lineman", 200, 60, edges, [])
    # certificate expires before the unstarted task begins
    expired = crew(certs=[Certification("lineman", 100.0)])
    historical, still_valid, invalid = revalidate([old, future], [expired], edges, now_min=150)
    assert historical == [old]
    assert still_valid == []
    assert invalid == [(future, "certificate expired")]
    # historical basis still records the cert expiry it was approved under
    assert historical[0].basis.cert_expires_min == 500.0


def test_revalidate_shift_change_only_hits_unstarted():
    edges = [RoadEdge("base", "job", 10)]
    day = crew(shift=(0, 480), certs=[Certification("lineman")])
    old = dispatch_contractor("d1", "j1", [day], "job", "lineman", 0, 60, edges, [])
    future = dispatch_contractor("d2", "j2", [day], "job", "lineman", 400, 60, edges, [])
    # shift moved to start at 460: historical task untouched, future task invalid
    swing = crew(shift=(460, 720), certs=[Certification("lineman")])
    historical, still_valid, invalid = revalidate([old, future], [swing], edges, now_min=150)
    assert historical == [old]
    assert invalid == [(future, "shift changed")]


def test_revalidate_road_blocked_and_crew_removed():
    edges = [RoadEdge("base", "job", 10)]
    c = crew(certs=[Certification("lineman")])
    future = dispatch_contractor("d1", "j1", [c], "job", "lineman", 200, 60, edges, [])
    blocked = [RoadEdge("base", "job", 10, blocked=True)]
    historical, valid, invalid = revalidate([future], [c], blocked, now_min=150)
    assert invalid == [(future, "road blocked")]
    _, _, gone = revalidate([future], [], edges, now_min=150)
    assert gone == [(future, "crew removed from roster")]


def test_history_lookup():
    edges = [RoadEdge("base", "job", 10)]
    c1 = crew("c1", unit="UnitA", certs=[Certification("lineman")])
    c2 = crew("c2", unit="UnitB", certs=[Certification("lineman")])
    r1 = dispatch_contractor("d1", "j1", [c1], "job", "lineman", 0, 60, edges, [])
    r2 = dispatch_contractor("d2", "j2", [c2], "job", "lineman", 0, 60, edges, [])
    assert history_for([r1, r2], crew_id="c1") == [r1]
    assert history_for([r1, r2], unit="UnitB") == [r2]
    assert history_for([r1, r2], job_id="j1") == [r1]
