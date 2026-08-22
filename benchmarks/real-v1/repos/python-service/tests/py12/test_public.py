from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep

from realbench.events import EventProcessor
from realbench.models import EventState


def test_duplicate_event_effect_is_applied_once_under_concurrency() -> None:
    state = EventState()
    processor = EventProcessor(state)
    count = 0
    count_lock = Lock()

    def effect() -> None:
        nonlocal count
        sleep(0.005)
        with count_lock:
            count += 1

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(lambda _: processor.apply("evt-1", effect), range(100)))
    assert sum(outcomes) == 1
    assert count == 1
    assert state.applied == ["evt-1"]
