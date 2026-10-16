from __future__ import annotations

import threading

from ringleader.home.jobs import JobBook, JobMessage


def test_a_landed_side_job_is_forgotten_by_the_book():
    posted: list[JobMessage] = []
    arrived = threading.Event()

    def post(message):
        posted.append(message)
        arrived.set()

    book = JobBook(post)
    results: list[object] = []
    token = book.run_side(lambda _token, _progress: 7, name="rl-test", on_result=results.append)
    assert arrived.wait(5)
    assert book._side_tokens == {token}
    posted[0].deliver()
    assert (book._side_tokens, len(results)) == (set(), 1)
