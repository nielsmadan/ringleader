from ringleader.decisions import ArchiveOp, ClassifyOp, DeleteOp, KeepOp, Outcome, PruneOp
from ringleader.home import overlay
from ringleader.home.overlay import Hidden
from ringleader.revalidate import Seen

A, B, C = ("claude-code", "a"), ("claude-code", "b"), ("codex", "c")
OK = Outcome(ok=True, message="ok", wrote=True)


def _seen(identity):
    return Seen(identity, identity[1], "m", None)


def _thread(identity):
    return {"source": identity[0], "sessionId": identity[1]}


def test_submitted_hides_each_ops_identities_as_queued():
    tickets = [(1, ArchiveOp(_seen(A))), (2, PruneOp((_seen(B), _seen(C)))), (3, ClassifyOp(()))]
    assert overlay.submitted({}, tickets) == {
        A: Hidden(1, "queued"),
        B: Hidden(2, "queued"),
        C: Hidden(2, "queued"),
    }


def test_submitted_replaces_a_held_harvest_job():
    held = overlay.held({}, A, -1)
    assert held == {A: Hidden(-1, "queued")}
    assert overlay.submitted(held, [(4, ArchiveOp(_seen(A)))]) == {A: Hidden(4, "queued")}


def test_started_marks_only_that_tickets_entries_running():
    current = {A: Hidden(1, "queued"), B: Hidden(2, "queued")}
    assert overlay.started(current, 1) == {A: Hidden(1, "running"), B: Hidden(2, "queued")}


def test_finished_marks_the_ticket_applied_at_its_generation():
    current = {A: Hidden(1, "running"), B: Hidden(2, "queued")}
    assert overlay.finished(current, 1, ArchiveOp(_seen(A)), OK, 7) == {
        A: Hidden(1, "applied", 7),
        B: Hidden(2, "queued"),
    }


def test_a_failed_op_that_wrote_nothing_comes_back():
    current = {A: Hidden(1, "running"), B: Hidden(2, "queued")}
    failed = Outcome(ok=False, message="gone", wrote=False)
    assert overlay.finished(current, 1, ArchiveOp(_seen(A)), failed, 3) == {B: Hidden(2, "queued")}


def test_a_refused_identity_in_a_bulk_op_comes_back_while_the_others_stay_hidden():
    op = PruneOp((_seen(A), _seen(B), _seen(C)))
    current = {key: Hidden(5, "running") for key in (A, B, C)}
    outcome = Outcome(ok=True, message="deleted 2", wrote=True, refused=((_seen(B), "changed"),))
    assert overlay.finished(current, 5, op, outcome, 2) == {
        A: Hidden(5, "applied", 2),
        C: Hidden(5, "applied", 2),
    }


def test_a_refusal_leaves_a_later_tickets_entry_for_the_same_identity():
    current = {A: Hidden(9, "queued")}
    refused = Outcome(ok=True, message="", wrote=True, refused=((_seen(A), "changed"),))
    assert overlay.finished(current, 5, PruneOp((_seen(A),)), refused, 1) == current


def test_removed_drops_the_given_tickets():
    current = {A: Hidden(1, "queued"), B: Hidden(2, "queued"), C: Hidden(-1, "queued")}
    assert overlay.removed(current, [1, -1]) == {B: Hidden(2, "queued")}


def test_adopting_a_snapshot_reveals_applied_rows_it_already_reflects():
    current = {A: Hidden(1, "applied", 3), B: Hidden(2, "running"), C: Hidden(4, "applied", 5)}
    assert overlay.adopted(current, 4) == {B: Hidden(2, "running"), C: Hidden(4, "applied", 5)}


def test_a_snapshot_older_than_the_applied_generation_keeps_the_row_hidden():
    current = {A: Hidden(1, "applied", 6)}
    assert overlay.adopted(current, 5) == current


def test_visible_drops_hidden_threads_without_touching_the_snapshot():
    reg = {"lastScan": "t", "threads": [_thread(A), _thread(B), _thread(C)]}
    view = overlay.visible(reg, {B: Hidden(1, "queued")})
    assert view == {"lastScan": "t", "threads": [_thread(A), _thread(C)]}
    assert len(reg["threads"]) == 3


def test_a_delete_round_trip_through_every_state():
    op = DeleteOp(_seen(A))
    queued = overlay.submitted({}, [(1, op)])
    running = overlay.started(queued, 1)
    applied = overlay.finished(running, 1, op, OK, 1)
    assert [queued, running, applied, overlay.adopted(applied, 1)] == [
        {A: Hidden(1, "queued")},
        {A: Hidden(1, "running")},
        {A: Hidden(1, "applied", 1)},
        {},
    ]


def test_removing_a_harvest_job_leaves_other_entries():
    held = overlay.held({}, A, -2)
    kept = overlay.submitted(held, [(3, KeepOp(_seen(B)))])
    assert overlay.removed(kept, [-2]) == {B: Hidden(3, "queued")}
