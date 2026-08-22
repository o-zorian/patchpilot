from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

from realbench.events import EventProcessor
from realbench.models import EventState


def test_two_distinct_events_each_apply_once() -> None:
    state = EventState()
    processor = EventProcessor(state)
    counts = {"a": 0, "b": 0}
    lock = Lock()
    barrier = Barrier(20)

    def invoke(index: int) -> bool:
        event_id = "a" if index % 2 == 0 else "b"
        barrier.wait()

        def effect() -> None:
            sleep(0.005)
            with lock:
                counts[event_id] += 1

        return processor.apply(event_id, effect)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(invoke, range(20)))
    assert counts == {"a": 1, "b": 1}
    assert sorted(state.applied) == ["a", "b"]
