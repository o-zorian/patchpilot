from realbench.models import Job
from realbench.scheduler import order_jobs


def test_equal_priority_uses_submission_order() -> None:
    jobs = [Job("first", 2, 10), Job("second", 2, 20), Job("urgent", 3, 30)]
    assert [job.id for job in order_jobs(jobs)] == ["urgent", "first", "second"]
