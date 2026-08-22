from realbench.models import Job
from realbench.scheduler import order_jobs


def test_sort_is_stable_when_priority_and_timestamp_match() -> None:
    jobs = [Job("a", 1, 5), Job("b", 1, 5), Job("c", 2, 9)]
    assert [job.id for job in order_jobs(jobs)] == ["c", "a", "b"]
