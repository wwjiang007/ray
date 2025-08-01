import asyncio
import importlib
import os
import random
import sys
import time
from typing import Optional, Set

import pytest

import ray
from ray._common.test_utils import async_wait_for_condition
from ray._common.utils import get_or_create_event_loop
from ray.actor import ActorHandle
from ray.exceptions import ActorDiedError, ActorUnavailableError
from ray.serve._private.common import (
    DeploymentHandleSource,
    DeploymentID,
    ReplicaID,
    RequestMetadata,
)
from ray.serve._private.constants import RAY_SERVE_QUEUE_LENGTH_CACHE_TIMEOUT_S
from ray.serve._private.replica_result import ReplicaResult
from ray.serve._private.request_router import (
    PendingRequest,
    PowerOfTwoChoicesRequestRouter,
    RunningReplica,
)
from ray.serve._private.request_router.common import ReplicaQueueLengthCache
from ray.serve._private.test_utils import MockTimer
from ray.serve._private.utils import generate_request_id

TIMER = MockTimer()

DEFAULT_MAX_ONGOING_REQUESTS = 10
ROUTER_NODE_ID = "router_node_id"
ROUTER_AZ = "router_az"


class FakeRunningReplica(RunningReplica):
    def __init__(
        self,
        replica_unique_id: str,
        *,
        node_id: str = "",
        availability_zone: Optional[str] = None,
        reset_after_response: bool = False,
        model_ids: Optional[Set[str]] = None,
        sleep_time_s: float = 0.0,
        max_ongoing_requests: int = DEFAULT_MAX_ONGOING_REQUESTS,
    ):
        self._replica_id = ReplicaID(
            unique_id=replica_unique_id,
            deployment_id=DeploymentID(name="TEST_DEPLOYMENT"),
        )
        self._node_id = node_id
        self._availability_zone = availability_zone
        self._queue_len = 0
        self._max_ongoing_requests = max_ongoing_requests
        self._has_queue_len_response = asyncio.Event()
        self._reset_after_response = reset_after_response
        self._model_ids = model_ids or set()
        self._sleep_time_s = sleep_time_s

        self.get_queue_len_was_cancelled = False
        self.queue_len_deadline_history = list()
        self.num_get_queue_len_calls = 0

    @property
    def replica_id(self) -> ReplicaID:
        return self._replica_id

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def availability_zone(self) -> Optional[str]:
        return self._availability_zone

    @property
    def multiplexed_model_ids(self) -> Set[str]:
        return self._model_ids

    @property
    def max_ongoing_requests(self) -> int:
        return self._max_ongoing_requests

    def set_queue_len_response(
        self,
        queue_len: int,
        exception: Optional[Exception] = None,
    ):
        self._queue_len = queue_len
        self._exception = exception
        self._has_queue_len_response.set()

    def push_proxy_handle(self, handle: ActorHandle):
        pass

    async def get_queue_len(self, *, deadline_s: float) -> int:
        self.num_get_queue_len_calls += 1
        self.queue_len_deadline_history.append(deadline_s)
        try:
            while not self._has_queue_len_response.is_set():
                await self._has_queue_len_response.wait()

            if self._sleep_time_s > 0:
                await asyncio.sleep(self._sleep_time_s)

            if self._reset_after_response:
                self._has_queue_len_response.clear()

            if self._exception is not None:
                raise self._exception

            return self._queue_len
        except asyncio.CancelledError:
            self.get_queue_len_was_cancelled = True
            raise

    def send_request(self, pr: PendingRequest) -> ReplicaResult:
        raise NotImplementedError()

    def send_request_with_rejection(self, pr: PendingRequest) -> ReplicaResult:
        raise NotImplementedError()


@pytest.fixture
def pow_2_router(request) -> PowerOfTwoChoicesRequestRouter:
    if not hasattr(request, "param"):
        request.param = {}

    # In order to prevent issues like https://github.com/ray-project/ray/issues/40631,
    # construct the request router on a different loop to mimic the deployment handle path.
    async def construct_request_router(loop: asyncio.AbstractEventLoop):
        request_router = PowerOfTwoChoicesRequestRouter(
            deployment_id=DeploymentID(name="TEST_DEPLOYMENT"),
            handle_source=request.param.get(
                "handle_source", DeploymentHandleSource.REPLICA
            ),
            prefer_local_node_routing=request.param.get("prefer_local_node", False),
            prefer_local_az_routing=request.param.get("prefer_local_az", False),
            self_node_id=ROUTER_NODE_ID,
            self_actor_id="fake-actor-id",
            self_actor_handle=None,
            self_availability_zone=request.param.get("az", None),
            use_replica_queue_len_cache=request.param.get(
                "use_replica_queue_len_cache", False
            ),
            get_curr_time_s=TIMER.time,
        )
        request_router.backoff_sequence_s = request.param.get(
            "backoff_sequence_s",
            [0, 0.001, 0.001, 0.001, 0.001, 0.001, 0.001],
        )
        return request_router

    s = asyncio.new_event_loop().run_until_complete(
        construct_request_router(get_or_create_event_loop())
    )

    # Update the RAY_SERVE_MULTIPLEXED_MODEL_ID_MATCHING_TIMEOUT_S
    # to 0.01s to speed up the test.
    os.environ.update({"RAY_SERVE_MULTIPLEXED_MODEL_ID_MATCHING_TIMEOUT_S": "0.01"})
    importlib.reload(ray.serve._private.constants)
    importlib.reload(ray.serve._private.request_router.request_router)

    # Reset mock timer to avoid state leakage.
    TIMER.reset()

    yield s

    # Always verify that all routing tasks exit once all queries are satisfied.
    assert s.curr_num_routing_tasks == 0
    assert s.num_pending_requests == 0


def fake_pending_request(
    *, created_at: Optional[float] = None, model_id: str = ""
) -> PendingRequest:
    if created_at is not None:
        return PendingRequest(
            args=list(),
            kwargs=dict(),
            metadata=RequestMetadata(
                request_id=generate_request_id(),
                internal_request_id=generate_request_id(),
                multiplexed_model_id=model_id,
            ),
            created_at=created_at,
        )
    else:
        return PendingRequest(
            args=list(),
            kwargs=dict(),
            metadata=RequestMetadata(
                request_id=generate_request_id(),
                internal_request_id=generate_request_id(),
                multiplexed_model_id=model_id,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_no_replicas_available_then_one_available(pow_2_router):
    """
    If there are replicas available, we should wait until one is added. Once a
    replica is added via `update_replicas`, the pending assignment should be fulfilled.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)
    s.update_replicas([r1])

    assert (await task) == r1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_replica_does_not_accept_then_accepts(pow_2_router):
    """
    If none of the replicas accept the request, we should repeatedly try with backoff.
    Once one accepts, the pending assignment should be fulfilled.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1])

    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    r1.set_queue_len_response(0)
    assert (await task) == r1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_no_replicas_accept_then_new_one_accepts(pow_2_router):
    """
    If none of the replicas accept the request, we should repeatedly try with backoff.
    Once one accepts, the pending assignment should be fulfilled.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1])

    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(0)
    s.update_replicas([r1, r2])

    assert (await task) == r2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_one_replica_available_then_none_then_one(pow_2_router):
    """
    If a replica stops accepting requests, it should stop being routed. When it then
    accepts, pending assignments should be routed on it.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1])

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    s.update_replicas([])
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    r1.set_queue_len_response(0)
    s.update_replicas([r1])

    assert (await task) == r1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_two_replicas_available_then_one(pow_2_router):
    """
    If two replicas are available and accepting requests, they should both get
    routed. If one is removed, only the other should be routed.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(0)

    s.update_replicas([r1, r2])

    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) in {r1, r2}

    s.update_replicas([r1])

    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) == r1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_two_replicas_one_accepts(pow_2_router):
    """
    If two replicas are available but only one accepts, only it should be routed.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)

    s.update_replicas([r1, r2])

    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) == r1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_three_replicas_two_accept(pow_2_router):
    """
    If three replicas are available but only two accept, only those should be routed.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)

    r3 = FakeRunningReplica("r3")
    r3.set_queue_len_response(0)

    s.update_replicas([r1, r2, r3])

    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) in {r1, r3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_two_replicas_choose_shorter_queue(pow_2_router):
    """
    If two replicas are available and accept requests, the one with the shorter
    queue should be routed.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(1)

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(0)

    s.update_replicas([r1, r2])

    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) == r2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_tasks_routed_fifo(pow_2_router):
    """
    Verify that requests are always routed in FIFO order, even if many are being
    assigned concurrently.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    # Route many requests in parallel; they cannot be fulfilled yet.
    tasks = []
    for _ in range(10):
        tasks.append(
            loop.create_task(s._choose_replica_for_request(fake_pending_request()))
        )

    done, _ = await asyncio.wait(tasks, timeout=0.01)
    assert len(done) == 0

    # Only a single request will be accepted at a time due to
    # `reset_after_response=True`.
    r1 = FakeRunningReplica("r1", reset_after_response=True)
    r1.set_queue_len_response(0)
    s.update_replicas([r1])

    # We need to wait until the initial ping from request router to replica
    # finishes, which then resets the events in the testing structure
    # so that the test can proceed.
    await async_wait_for_condition(lambda: not r1._has_queue_len_response.is_set())

    for _ in range(len(tasks)):
        r1.set_queue_len_response(0)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        # If the order was not FIFO, the fulfilled assignment may not be the front of
        # the list.
        assert done.pop() == tasks[0]
        tasks = tasks[1:]


@pytest.mark.asyncio
async def test_retried_tasks_routed_fifo(pow_2_router):
    """
    Verify that pending requests whose routing is retried are still routed in fifo
    order based on creation time, even if they are inserted in a different order.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    start = time.time()
    pending_requests = [fake_pending_request(created_at=start + i) for i in range(10)]

    random_order_index = list(range(len(pending_requests)))
    random.shuffle(random_order_index)

    # Route the requests in parallel; they cannot be fulfilled yet.
    tasks = []
    for idx in random_order_index:
        tasks.append(
            loop.create_task(
                s._choose_replica_for_request(pending_requests[idx], is_retry=True),
                name=f"request-{idx}",
            )
        )

    done, _ = await asyncio.wait(tasks, timeout=0.01)
    assert len(done) == 0

    # Only a single request will be accepted at a time due to
    # `reset_after_response=True`.
    r1 = FakeRunningReplica("r1", reset_after_response=True)
    r1.set_queue_len_response(0)
    s.update_replicas([r1])

    # We need to wait until the initial ping from request router to replica
    # finishes, which then resets the events in the testing structure
    # so that the test can proceed.
    await async_wait_for_condition(lambda: not r1._has_queue_len_response.is_set())

    # Check that the tasks are routed in the order they were created (not the.
    # order they were retried).
    for expected_idx in range(len(pending_requests)):
        r1.set_queue_len_response(0)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == 1

        t = done.pop()
        assert t.get_name() == f"request-{expected_idx}"
        tasks.remove(t)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_cancellation(pow_2_router):
    """
    If a pending assignment is cancelled, it shouldn't get fulfilled and the next
    request in the queue should be.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    task1 = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    task2 = loop.create_task(s._choose_replica_for_request(fake_pending_request()))

    done, _ = await asyncio.wait([task1, task2], timeout=0.01)
    assert len(done) == 0

    task1.cancel()

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)
    s.update_replicas([r1])

    assert (await task2) == r1

    # Verify that the routing tasks exit and there are no assignments left.
    assert s.curr_num_routing_tasks == 0
    assert s.num_pending_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_cancellation_when_replicas_maxed(pow_2_router):
    """
    If a pending assignment is cancelled, it shouldn't get fulfilled and the next
    request in the queue should be.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))

    # There is only one replica that is maxed out on requests
    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS)
    s.update_replicas([r1])
    # So one routing task should have been started to try to route
    # the request to a replica, but it should be blocked because the
    # replica doesn't have capacity to accept new requests
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0
    assert s.curr_num_routing_tasks == 1

    # Cancel while the routing task is repeatedly trying to find an
    # available replica
    task.cancel()

    # Verify that the routing tasks exit and there are no assignments left.
    await async_wait_for_condition(
        lambda: s.curr_num_routing_tasks == 0, retry_interval_ms=1
    )
    assert s.num_pending_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_only_task_cancelled(pow_2_router):
    """
    If a pending assignment is cancelled and it's the only one in the queue, it should
    be passed over and the routing task should exit.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))

    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    task.cancel()

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)
    s.update_replicas([r1])

    start = time.time()
    while time.time() - start < 10:
        # Verify that the routing task exits and there are no assignments left.
        if s.curr_num_routing_tasks == 0 and s.num_pending_requests == 0:
            break
        await asyncio.sleep(0.1)
    else:
        raise TimeoutError(
            "Routing task and pending assignment still around after 10s."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_routing_task_cap(pow_2_router):
    """
    Verify that the number of routing tasks never exceeds the cap (2 * num_replicas).
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    tasks = []
    for _ in range(10):
        tasks.append(
            loop.create_task(s._choose_replica_for_request(fake_pending_request()))
        )

    done, _ = await asyncio.wait(tasks, timeout=0.01)
    assert len(done) == 0

    # There should be zero routing tasks while there are no replicas.
    assert s.curr_num_routing_tasks == 0

    r1 = FakeRunningReplica("r1", reset_after_response=True)
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1])

    done, _ = await asyncio.wait(tasks, timeout=0.01)
    assert len(done) == 0

    # Now that there is at least one replica available, there should be nonzero
    # number of tasks running.
    assert s.curr_num_routing_tasks > 0
    assert s.curr_num_routing_tasks == s.max_num_routing_tasks

    # Number of tasks should increase when more replicas are available.
    routing_tasks_one_replica = s.curr_num_routing_tasks
    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1, r2])
    assert s.curr_num_routing_tasks > routing_tasks_one_replica
    assert s.curr_num_routing_tasks == s.max_num_routing_tasks

    # Number of tasks should decrease as the number of pending queries decreases.
    for i in range(len(tasks)):
        r1.set_queue_len_response(0)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        assert done.pop() == tasks[0]
        tasks = tasks[1:]

        assert s.curr_num_routing_tasks == min(len(tasks), s.max_num_routing_tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_routing_task_cap_hard_limit(pow_2_router):
    """
    Verify that the number of routing tasks never exceeds the hard limit if set.
    """
    s = pow_2_router
    hard_limit = 2
    s.max_num_routing_tasks_cap = hard_limit

    loop = get_or_create_event_loop()

    tasks = []
    for _ in range(10):
        tasks.append(
            loop.create_task(s._choose_replica_for_request(fake_pending_request()))
        )

    done, _ = await asyncio.wait(tasks, timeout=0.01)
    assert len(done) == 0

    # There should be zero routing tasks while there are no replicas.
    assert s.curr_num_routing_tasks == 0

    r1 = FakeRunningReplica("r1", reset_after_response=True)
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1])

    done, _ = await asyncio.wait(tasks, timeout=0.01)
    assert len(done) == 0

    # Now that there is at least one replica available, there should be nonzero
    # number of tasks running.
    assert s.curr_num_routing_tasks > 0
    assert s.curr_num_routing_tasks == 2

    # Number of tasks should not increase when adding another replica due to the limit.
    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    s.update_replicas([r1, r2])
    assert s.curr_num_routing_tasks == hard_limit

    # Number of tasks should decrease as the number of pending queries decreases.
    for i in range(len(tasks)):
        r1.set_queue_len_response(0)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        assert done.pop() == tasks[0]
        tasks = tasks[1:]

        assert s.curr_num_routing_tasks == min(len(tasks), hard_limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_replica_responds_after_being_removed(pow_2_router):
    """
    Verify that if a replica is removed from the active set while the queue length
    message is in flight, it won't be routed and a new replica will be.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    # Set a very high response deadline to ensure we can have the replica respond after
    # calling `update_replicas`.
    s.queue_len_response_deadline_s = 100

    r1 = FakeRunningReplica("r1")
    s.update_replicas([r1])

    # Start the routing task, which will hang waiting for the queue length response.
    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))

    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0
    assert s.curr_num_routing_tasks == 1

    # Update the replicas to remove the existing replica and add a new one.
    # Also set the queue length response on the existing replica.
    r2 = FakeRunningReplica("r2")
    s.update_replicas([r2])
    r1.set_queue_len_response(0)

    # The original replica should *not* be routed.
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0
    assert s.curr_num_routing_tasks == 1

    # Set the new replica to accept, it should be routed.
    r2.set_queue_len_response(0)
    assert (await task) == r2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
    ],
    indirect=True,
)
async def test_prefer_replica_on_same_node(pow_2_router):
    """
    Verify that the request router prefers replicas that are colocated on the same node ID
    as itself. If the first candidate replicas on the same node reject the request,
    it should fall back to all replicas.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1", node_id=ROUTER_NODE_ID)
    r1.set_queue_len_response(0)
    r2 = FakeRunningReplica("r2", node_id="some_other_node_in_the_stratosphere")
    r2.set_queue_len_response(0)
    s.update_replicas([r1, r2])

    tasks = []
    for _ in range(10):
        tasks.append(
            loop.create_task(s._choose_replica_for_request(fake_pending_request()))
        )

    # All requests should be routed to the replica on the same node if it accepts.
    assert all(replica == r1 for replica in await asyncio.gather(*tasks))

    # Update the replica on the same node to reject requests -- now requests should
    # fall back to the other replica.
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)

    tasks = []
    for _ in range(10):
        tasks.append(
            loop.create_task(s._choose_replica_for_request(fake_pending_request()))
        )

    # All requests should be routed to the other replica.
    assert all(replica == r2 for replica in await asyncio.gather(*tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [{"prefer_local_node": True, "prefer_local_az": True, "az": ROUTER_AZ}],
    indirect=True,
)
async def test_prefer_replica_in_same_az(pow_2_router):
    """
    When prefer routing on same node and prefer routing to same AZ is
    on, verify that the request router prefers
    * replicas that are colocated on the same node
    * then replicas that are colocated in the same AZ
    * lastly fall back to all replicas
    """

    s = pow_2_router
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1", node_id=ROUTER_NODE_ID, availability_zone=ROUTER_AZ)
    r2 = FakeRunningReplica(
        "r2",
        node_id="some_other_node_in_the_stratosphere",
        availability_zone=ROUTER_AZ,
    )
    r3 = FakeRunningReplica(
        "r3",
        node_id="some_other_node_in_the_stratosphere",
        availability_zone="some_other_az_in_the_solar_system",
    )
    r1.set_queue_len_response(0)
    r2.set_queue_len_response(0)
    r3.set_queue_len_response(0)
    s.update_replicas([r1, r2, r3])

    async def choose_replicas():
        tasks = []
        for _ in range(10):
            tasks.append(
                loop.create_task(s._choose_replica_for_request(fake_pending_request()))
            )
        return await asyncio.gather(*tasks)

    # All requests should be routed to the replica on the same node if it accepts.
    assert all(replica == r1 for replica in await choose_replicas())

    # Update the replica on the same node to reject requests -- now requests should
    # fall back to replica in the same az.
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    assert all(replica == r2 for replica in await choose_replicas())

    # Update the replica on the same az to reject requests -- now requests should
    # fall back to the last replica.
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    assert all(replica == r3 for replica in await choose_replicas())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [{"prefer_local_az": False, "az": ROUTER_AZ}],
    indirect=True,
)
async def test_prefer_az_off(pow_2_router):
    """
    When prefer routing to same AZ is OFF, verify that requests are
    spread to replicas across AZs
    """

    s = pow_2_router
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1", availability_zone=ROUTER_AZ)
    r2 = FakeRunningReplica("r2", availability_zone=ROUTER_AZ)
    r3 = FakeRunningReplica("r3", availability_zone="western-hemisphere")
    r1.set_queue_len_response(0)
    r2.set_queue_len_response(0)
    r3.set_queue_len_response(0)
    s.update_replicas([r1, r2, r3])

    async def choose_replicas():
        tasks = []
        for _ in range(10):
            tasks.append(
                loop.create_task(s._choose_replica_for_request(fake_pending_request()))
            )
        replicas = await asyncio.gather(*tasks)
        return {r.replica_id for r in replicas}

    async def verify_replicas_batched(expected_replicas: Set[str]):
        chosen_replicas = set()
        for _ in range(100):
            chosen_replicas = chosen_replicas.union(await choose_replicas())
            print("Replicas chosen after batch of 10:", chosen_replicas)
            if chosen_replicas == expected_replicas:
                break
        assert chosen_replicas == expected_replicas

    # Requests should be spread across all nodes
    # NOTE(zcin): Choose up to 1000 replicas in batches of 10 at a time.
    # This deflakes the test, but also makes sure the test runs fast on average
    await verify_replicas_batched({r1.replica_id, r2.replica_id, r3.replica_id})

    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    await verify_replicas_batched({r2.replica_id, r3.replica_id})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [{"prefer_local_node": False, "prefer_local_az": True, "az": ROUTER_AZ}],
    indirect=True,
)
async def test_prefer_replica_in_same_az_without_prefer_node(pow_2_router):
    """
    When prefer routing on same node is OFF and prefer routing to same
    AZ is ON, verify that the request router prefers
    * replicas that are colocated in the same AZ
    * then fall back to all replicas
    """

    s = pow_2_router
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1", node_id=ROUTER_NODE_ID, availability_zone=ROUTER_AZ)
    r2 = FakeRunningReplica("r2", node_id="node-alpha", availability_zone=ROUTER_AZ)
    r3 = FakeRunningReplica("r3", node_id="node-beta", availability_zone="some_zone")
    r1.set_queue_len_response(0)
    r2.set_queue_len_response(0)
    r3.set_queue_len_response(0)
    s.update_replicas([r1, r2, r3])

    async def choose_replicas():
        tasks = []
        for _ in range(10):
            tasks.append(
                loop.create_task(s._choose_replica_for_request(fake_pending_request()))
            )
        return await asyncio.gather(*tasks)

    # All requests should be routed to the two nodes in the same AZ
    # (r1 and r2). Without node preference in routing, requests should
    # be routed to BOTH r1 and r2
    assert set(await choose_replicas()) == {r1, r2}

    # Update replica on one of the nodes in the same AZ to reject
    # requests. Now requests should only go to the remaining node in the
    # same AZ
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    assert all(replica == r1 for replica in await choose_replicas())

    # Update the replica on last node in the same AZ to reject requests.
    # Now requests should fall back to the last replica.
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    assert all(replica == r3 for replica in await choose_replicas())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [{"prefer_local_node": True, "prefer_local_az": False, "az": ROUTER_AZ}],
    indirect=True,
)
async def test_prefer_replica_on_same_node_without_prefer_az(pow_2_router):
    """
    When prefer routing to same node is ON and prefer routing to same AZ
    is OFF, verify that requests are first routed to same-node
    replicas, then spread across all availability zones.
    """

    s = pow_2_router
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica(
        "r1", node_id=ROUTER_NODE_ID, availability_zone=ROUTER_AZ
    )  # noqa
    r2 = FakeRunningReplica("r2", node_id="node-alpha", availability_zone=ROUTER_AZ)
    r3 = FakeRunningReplica("r3", node_id="node-beta", availability_zone="west")
    r1.set_queue_len_response(0)
    r2.set_queue_len_response(0)
    r3.set_queue_len_response(0)
    s.update_replicas([r1, r2, r3])

    async def choose_replicas():
        tasks = []
        for _ in range(10):
            tasks.append(
                loop.create_task(s._choose_replica_for_request(fake_pending_request()))
            )
        return await asyncio.gather(*tasks)

    # Requests should be sent to replica on same node
    assert all(replica == r1 for replica in await choose_replicas())

    # If replica on same node is blocked, there should be no preference between
    # remaining replicas even if the availability zones are different.
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    assert set(await choose_replicas()) == {r2, r3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
        {"prefer_local_node": True, "prefer_local_az": False},
        {"prefer_local_node": False, "prefer_local_az": True},
        {"prefer_local_node": False, "prefer_local_az": False},
    ],
    indirect=True,
)
class TestModelMultiplexing:
    async def test_replicas_with_model_id_always_chosen(self, pow_2_router):
        """
        Verify that if accepted, only replicas with a given model ID will be chosen.
        This should be independent of queue length.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        r1 = FakeRunningReplica("r1", model_ids={"m1", "m2"})
        r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS - 1)
        r2 = FakeRunningReplica("r2", model_ids={"m2", "m3"})
        r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS - 1)
        r3 = FakeRunningReplica("r3", model_ids={})
        r3.set_queue_len_response(0)
        s.update_replicas([r1, r2, r3])

        for _ in range(10):
            request = fake_pending_request(model_id="m2")
            task = loop.create_task(s._choose_replica_for_request(request))
            assert (await task) in {r1, r2}

    async def test_choose_least_number_of_models_replicas(self, pow_2_router):
        """
        If no replica has the model_id, choose the least number of models replicas.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()
        r1 = FakeRunningReplica("r1", model_ids={"m1", "m2"})
        r2 = FakeRunningReplica("r2", model_ids={"m2"})
        r1.set_queue_len_response(0)
        r2.set_queue_len_response(0)
        s.update_replicas([r1, r2])
        for _ in range(10):
            request = fake_pending_request(model_id="m3")
            task = loop.create_task(s._choose_replica_for_request(request))
            assert (await task) == r2

    async def test_backoff_from_least_number_of_models_replicas(self, pow_2_router):
        """
        If no replica has the model_id, choose the least number of models replicas.
        If those replicas cannot be routed to, we should fall back to all replicas.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()
        r1 = FakeRunningReplica("r1", model_ids={"m1", "m2"})
        r2 = FakeRunningReplica("r2", model_ids={"m2"})
        r1.set_queue_len_response(0)
        r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
        s.update_replicas([r1, r2])
        for _ in range(10):
            request = fake_pending_request(model_id="m3")
            task = loop.create_task(s._choose_replica_for_request(request))
            assert (await task) == r1

    async def test_no_replica_has_model_id(self, pow_2_router):
        """
        If no replica has the model_id, we should fall back to normal procedure.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        r1 = FakeRunningReplica("r1", model_ids={})
        r1.set_queue_len_response(0)
        s.update_replicas([r1])

        for _ in range(10):
            request = fake_pending_request(model_id="m1")
            task = loop.create_task(s._choose_replica_for_request(request))
            assert (await task) == r1

    async def test_fall_back_to_replica_without_model_id(self, pow_2_router):
        """
        Verify that we'll fall back to a replica that doesn't have the model ID if
        none of the replicas with it can accept the request.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        r1 = FakeRunningReplica("r1", model_ids={"m1", "m2"})
        r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
        r2 = FakeRunningReplica("r2", model_ids={"m2", "m3"})
        r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
        r3 = FakeRunningReplica("r3", model_ids={})
        r3.set_queue_len_response(0)
        s.update_replicas([r1, r2, r3])

        for _ in range(10):
            request = fake_pending_request(model_id="m2")
            task = loop.create_task(s._choose_replica_for_request(request))
            assert (await task) == r3

    async def test_multiple_queries_with_different_model_ids(self, pow_2_router):
        """
        Verify that multiple queries with different model_ids will be mapped to the
        appropriate replicas.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        r1 = FakeRunningReplica("r1", model_ids={"m1"})
        r1.set_queue_len_response(0)
        r2 = FakeRunningReplica("r2", model_ids={"m2"})
        r2.set_queue_len_response(0)
        r3 = FakeRunningReplica("r3", model_ids={"m3"})
        r3.set_queue_len_response(0)
        s.update_replicas([r1, r2, r3])

        for _ in range(10):
            tasks = [
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m1"))
                ),
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m2"))
                ),
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m3"))
                ),
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m1"))
                ),
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m2"))
                ),
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m3"))
                ),
            ]

            done, _ = await asyncio.wait(tasks, timeout=0.1)
            assert len(done) == len(tasks)

            assert all(
                [
                    tasks[0].result() == r1,
                    tasks[1].result() == r2,
                    tasks[2].result() == r3,
                    tasks[3].result() == r1,
                    tasks[4].result() == r2,
                    tasks[5].result() == r3,
                ]
            )

    async def test_no_replicas_available_then_choose_one_with_id(self, pow_2_router):
        """
        Verify that if new replicas are added while the routing task is in backoff,
        it will prioritize those with the model ID.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        r1 = FakeRunningReplica("r1")
        r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)

        tasks = [
            loop.create_task(
                s._choose_replica_for_request(fake_pending_request(model_id="m1"))
            )
            for _ in range(100)
        ]

        # Routing tasks should be in backoff.
        done, _ = await asyncio.wait(tasks, timeout=0.01)
        assert len(done) == 0

        # Now add two more replicas, one of which has the model ID.
        # That one should be chosen for all of the tasks.
        r2 = FakeRunningReplica("r2")
        r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
        r3 = FakeRunningReplica("r3", model_ids={"m1"})
        r3.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS - 1)

        s.update_replicas([r1, r2, r3])

        assert all(replica == r3 for replica in await asyncio.gather(*tasks))

    @pytest.mark.asyncio
    async def test_tasks_routed_fifo_among_model_ids(self, pow_2_router):
        """
        Verify that requests are routed FIFO based on model ID.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        # Route many requests to each model ID in parallel
        # that cannot be fulfilled yet.
        m1_tasks = []
        m2_tasks = []
        for _ in range(10):
            m1_tasks.append(
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m1"))
                )
            )
            m2_tasks.append(
                loop.create_task(
                    s._choose_replica_for_request(fake_pending_request(model_id="m2"))
                )
            )

        done, _ = await asyncio.wait(m1_tasks + m2_tasks, timeout=0.01)
        assert len(done) == 0

        r1 = FakeRunningReplica("r1", model_ids={"m1"}, reset_after_response=True)
        r1.set_queue_len_response(0)
        r2 = FakeRunningReplica("r2", model_ids={"m2"}, reset_after_response=True)
        r2.set_queue_len_response(0)
        s.update_replicas([r1, r2])

        # We need to wait until the initial ping from request router to replica
        # finishes, which then resets the events in the testing structure
        # so that the test can proceed.
        await async_wait_for_condition(
            lambda: not r1._has_queue_len_response.is_set(), retry_interval_ms=10
        )
        await async_wait_for_condition(
            lambda: not r2._has_queue_len_response.is_set(), retry_interval_ms=10
        )

        # In each iteration, allow one replica of w/ each model ID to be routed.
        # The tasks for each model ID should be routed in FIFO order.
        for _ in range(10):
            r1.set_queue_len_response(0)
            r2.set_queue_len_response(0)

            done, pending = await asyncio.wait(
                m1_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            assert done.pop() == m1_tasks[0]
            m1_tasks = m1_tasks[1:]

            done, pending = await asyncio.wait(
                m2_tasks, return_when=asyncio.FIRST_COMPLETED
            )
            assert done.pop() == m2_tasks[0]
            m2_tasks = m2_tasks[1:]

    async def test_replicas_with_model_id_not_chosen_when_busy(self, pow_2_router):
        """
        Setup 3 replicas, one of which has the model ID, the other two do not. Verifies
        that when the replica with the model ID is busy, the other replicas are chosen.
        """
        s = pow_2_router
        loop = get_or_create_event_loop()

        r1 = FakeRunningReplica("r1", model_ids={"m1"})
        r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS)
        r2 = FakeRunningReplica("r2", model_ids={})
        r2.set_queue_len_response(0)
        r3 = FakeRunningReplica("r3", model_ids={})
        r3.set_queue_len_response(0)
        s.update_replicas([r1, r2, r3])

        # Sending burst of requests with model_id=m1.
        tasks = [
            loop.create_task(
                s._choose_replica_for_request(fake_pending_request(model_id="m1"))
            )
            for _ in range(100)
        ]

        # Ensure that all tasks are routed to r2 and r3 right away, since r1 is busy.
        #
        # The timeout is important in this test, else the request can still wait for the
        # _multiplexed_matching_timeout to expire then to go to other replicas. This
        # timeout ensures that the request is routed to other replicas right away
        # after first try.
        done, _ = await asyncio.wait(tasks, timeout=0.1)
        assert len(done) == 100
        for task in done:
            assert task.result() in {r2, r3}


@pytest.mark.asyncio
async def test_get_queue_len_cancelled_on_timeout(pow_2_router):
    """
    Verify that `get_queue_len` is cancelled if the `queue_len_response_deadline_s`
    is reached.
    """
    s = pow_2_router
    s.queue_len_response_deadline_s = 0.001
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1")
    s.update_replicas([r1])

    # Attempt to route; the replica will be attempted and a timeout will occur
    # due to the short timeout set above.
    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.1)
    assert len(done) == 0

    # The `get_queue_len` method should be cancelled.
    assert r1.get_queue_len_was_cancelled

    r1.set_queue_len_response(0)
    assert (await task) == r1


@pytest.mark.asyncio
async def test_queue_len_response_deadline_backoff(pow_2_router):
    """
    Verify that the response deadline is exponentially backed off up to the max.
    """
    s = pow_2_router
    s.queue_len_response_deadline_s = 0.001
    s.max_queue_len_response_deadline_s = 0.005
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1")
    s.update_replicas([r1])

    # Attempt to route; the replica will be attempted and a timeout will occur
    # due to the short timeout set above.
    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    # Verify first ping
    assert r1.queue_len_deadline_history[0] == 0.001

    # Verify that the deadline never exceeds the max and deadline_n+1 is equal to
    # the max or 2*deadline_n.
    for i, j in zip(
        range(1, len(r1.queue_len_deadline_history) - 1),
        range(2, len(r1.queue_len_deadline_history)),
    ):
        deadline_i = r1.queue_len_deadline_history[i]
        deadline_j = r1.queue_len_deadline_history[j]
        assert (
            deadline_i <= deadline_j
            and deadline_j <= s.max_queue_len_response_deadline_s
        )
        if deadline_i < s.max_queue_len_response_deadline_s:
            assert (
                deadline_j == s.max_queue_len_response_deadline_s
                or deadline_j == 2 * deadline_i
            )

    r1.set_queue_len_response(0)
    assert (await task) == r1


@pytest.mark.asyncio
async def test_max_queue_len_response_deadline(pow_2_router):
    """
    Verify that if the max response deadline is > the initial deadline, the initial is
    always used.
    """
    s = pow_2_router
    s.queue_len_response_deadline_s = 0.01
    s.max_queue_len_response_deadline_s = 0.001
    loop = get_or_create_event_loop()

    r1 = FakeRunningReplica("r1")
    s.update_replicas([r1])

    # Attempt to route; the replica will be attempted and a timeout will occur
    # due to the short timeout set above.
    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.01)
    assert len(done) == 0

    assert all(
        d == s.queue_len_response_deadline_s for d in r1.queue_len_deadline_history
    )

    r1.set_queue_len_response(0)
    assert (await task) == r1


@pytest.mark.asyncio
async def test_replicas_updated_event_on_correct_loop(pow_2_router):
    """See https://github.com/ray-project/ray/issues/40631.

    The `await` statements below would fail with
    "RuntimeError: ... got Future <Future pending> attached to a different loop."
    """
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            pow_2_router._replicas_updated_event.wait(), timeout=0.001
        )

    pow_2_router._replicas_updated_event.set()
    await pow_2_router._replicas_updated_event.wait()


@pytest.mark.asyncio
async def test_queue_len_cache():
    TIMER.reset()

    staleness_timeout_s = 10.0
    c = ReplicaQueueLengthCache(
        staleness_timeout_s=staleness_timeout_s, get_curr_time_s=TIMER.time
    )

    d_id = DeploymentID(name="TEST_DEPLOYMENT")
    replica_id_1 = ReplicaID(
        "r1",
        deployment_id=d_id,
    )
    replica_id_2 = ReplicaID(
        "r2",
        deployment_id=d_id,
    )
    replica_id_3 = ReplicaID(
        "r3",
        deployment_id=d_id,
    )
    replica_id_4 = ReplicaID(
        "r4",
        deployment_id=d_id,
    )

    # Get nonexistent key.
    assert c.get(replica_id_1) is None

    # Insert and get a valid key.
    c.update(replica_id_1, 123)
    assert c.get(replica_id_1) == 123

    # Get timed out key.
    TIMER.advance(staleness_timeout_s + 1)
    assert c.get(replica_id_1) is None

    # Reset timed out key.
    c.update(replica_id_1, 456)
    assert c.get(replica_id_1) == 456

    # Insert multiple keys and remove an inactive set of them.
    c.update(replica_id_1, 1)
    c.update(replica_id_2, 2)
    c.update(replica_id_3, 3)
    c.update(replica_id_4, 4)
    c.remove_inactive_replicas(
        active_replica_ids={replica_id_1, replica_id_3},
    )
    assert all(
        [
            c.get(replica_id_1) == 1,
            c.get(replica_id_2) is None,
            c.get(replica_id_3) == 3,
            c.get(replica_id_4) is None,
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"use_replica_queue_len_cache": True},
    ],
    indirect=True,
)
async def test_queue_len_cache_active_probing(pow_2_router):
    """
    Verify that if a replica has a valid queue entry, it is not actively probed.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()
    staleness_timeout_s = RAY_SERVE_QUEUE_LENGTH_CACHE_TIMEOUT_S

    # Add an entry for replica "r1" -- it shouldn't be actively probed.
    r1 = FakeRunningReplica("r1")
    s.update_replicas([r1])
    s.replica_queue_len_cache.update(r1.replica_id, 0)

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.1)
    assert len(done) == 1
    assert (await task) == r1
    # 0 probes from routing requests
    # + 1 probe from when the replica set was updated with replica r1
    assert len(r1.queue_len_deadline_history) - 1 == 0

    # Now time out the entry in the cache -- replica should be probed.
    TIMER.advance(staleness_timeout_s + 1)
    r1.set_queue_len_response(0)

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.1)
    assert len(done) == 1
    assert (await task) == r1
    # 1 probe from routing requests
    # + 1 probe from when the replica set was updated with replica r1
    assert len(r1.queue_len_deadline_history) - 1 == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"use_replica_queue_len_cache": True},
    ],
    indirect=True,
)
async def test_queue_len_cache_replica_at_capacity_is_probed(pow_2_router):
    """
    Verify that if a replica has a cache entry but is at max_ongoing_requests, it's
    actively probed.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    # Add an entry for replica "r1" -- it shouldn't be actively probed.
    r1 = FakeRunningReplica("r1")
    s.update_replicas([r1])
    s.replica_queue_len_cache.update(r1.replica_id, DEFAULT_MAX_ONGOING_REQUESTS)

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.1)
    assert len(done) == 0
    # 1 probe from routing requests
    # + 1 probe from when the replica set was updated with replica r1
    assert len(r1.queue_len_deadline_history) - 1 == 1

    # Now let the replica respond and accept the request, it should be routed.
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS - 1)
    done, _ = await asyncio.wait([task], timeout=0.1)
    assert len(done) == 1
    assert (await task) == r1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"use_replica_queue_len_cache": True},
    ],
    indirect=True,
)
async def test_queue_len_cache_background_probing(pow_2_router):
    """
    Verify that if there are two replicas, one with a valid queue entry and one without,
    the one in the queue is chosen and the other is probed in the background.
    """
    s = pow_2_router
    loop = get_or_create_event_loop()

    # Add an entry for replica "r1" -- it shouldn't be actively probed.
    r1 = FakeRunningReplica("r1")
    r2 = FakeRunningReplica("r2")
    s.update_replicas([r1, r2])
    s.replica_queue_len_cache.update(r1.replica_id, 0)

    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))
    done, _ = await asyncio.wait([task], timeout=0.1)
    assert len(done) == 1
    assert (await task) == r1
    # 0 probes from routing requests
    # + 1 probe from when the replica set was updated with replica r1
    assert len(r1.queue_len_deadline_history) - 1 == 0

    r2.set_queue_len_response(3)

    def r2_was_probed():
        # Check that r2 was probed and the response was added to the cache.
        # 1 probe from routing requests
        # + 1 probe from when the replica set was updated with replica r1
        assert (
            len(r2.queue_len_deadline_history) - 1 == 1
            and s._replica_queue_len_cache.get(r2.replica_id) == 3
        )
        return True

    await async_wait_for_condition(r2_was_probed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"use_replica_queue_len_cache": True},
    ],
    indirect=True,
)
async def test_queue_len_cache_entries_added_correctly(pow_2_router):
    """
    Verify that the cache entries are updated for probed replicas correctly.
    """
    s = pow_2_router
    staleness_timeout_s = RAY_SERVE_QUEUE_LENGTH_CACHE_TIMEOUT_S

    r1 = FakeRunningReplica("r1")
    r2 = FakeRunningReplica("r2")
    s.update_replicas([r1, r2])

    for i in range(100):
        r1_queue_len = int(DEFAULT_MAX_ONGOING_REQUESTS * random.random())
        r2_queue_len = int(DEFAULT_MAX_ONGOING_REQUESTS * random.random())
        r1.set_queue_len_response(r1_queue_len)
        r2.set_queue_len_response(r2_queue_len)

        replica = await s._choose_replica_for_request(fake_pending_request())
        if r1_queue_len < r2_queue_len:
            assert replica == r1
        elif r2_queue_len < r1_queue_len:
            assert replica == r2
        else:
            assert replica in {r1, r2}

        # i+1 probes from routing requests
        # + 1 probe from when the replica set was updated with replica r1
        assert len(r1.queue_len_deadline_history) - 1 == i + 1
        assert len(r2.queue_len_deadline_history) - 1 == i + 1
        assert s._replica_queue_len_cache.get(r1.replica_id) == r1_queue_len
        assert s._replica_queue_len_cache.get(r2.replica_id) == r2_queue_len
        TIMER.advance(staleness_timeout_s + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"prefer_local_node": True, "prefer_local_az": True},
    ],
    indirect=True,
)
@pytest.mark.parametrize("backoff_index", [0, 10, 2048])
async def test_backoff_index_handling(pow_2_router, backoff_index: int):
    """Ensure that different ranges of backoff_index are valid.

    In the past, high backoff_indexes (greater than 1024) have caused
    OverflowErrors. See https://github.com/ray-project/ray/issues/43964.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(0)

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(0)

    s.update_replicas([r1, r2])

    r = await s._select_from_candidate_replicas([r1, r2], backoff_index)
    assert r in [r1, r2]


@pytest.mark.asyncio
@pytest.mark.parametrize("pow_2_router", [{}], indirect=True)
async def test_replicas_actor_died_error(
    pow_2_router: PowerOfTwoChoicesRequestRouter,
):
    """
    If replicas return an ActorDiedError, they should be removed from the
    local list.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(
        queue_len=0,
        exception=ActorDiedError(),
    )

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(0)

    s.update_replicas([r1, r2])

    # After detecting that the first replica died, the request router should
    # stop routing it.
    await s._choose_replica_for_request(fake_pending_request())
    assert set(pow_2_router.curr_replicas.values()) == {r2}

    # Check that get_queue_len is never called on r1 and always called on r2.
    r1.num_get_queue_len_calls = 0
    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) == r2
    assert r1.num_get_queue_len_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("pow_2_router", [{}], indirect=True)
async def test_replicas_actor_unavailable_error(
    pow_2_router: PowerOfTwoChoicesRequestRouter,
):
    """
    If replicas return an ActorUnavailableError, they should remain in the
    local list.
    """
    s = pow_2_router

    r1 = FakeRunningReplica("r1")
    r1.set_queue_len_response(1)
    r1.set_queue_len_response(
        queue_len=0,
        exception=ActorUnavailableError(
            error_message="Actor is temporarily unavailable",
            actor_id=b"a" * 16,
        ),
    )

    r2 = FakeRunningReplica("r2")
    r2.set_queue_len_response(5)

    s.update_replicas([r1, r2])

    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) == r2

    # The request router should keep r1 since it may recover.
    assert set(pow_2_router.curr_replicas.values()) == {r1, r2}

    # Restore r1.
    r1.set_queue_len_response(queue_len=0, exception=None)

    # The request router should keep picking r1 since it has a smaller queue length.
    for _ in range(10):
        assert (await s._choose_replica_for_request(fake_pending_request())) == r1


@pytest.mark.skipif(sys.platform == "win32", reason="Flaky on Windows")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {
            "prefer_local_node": True,
            "prefer_local_az": True,
            "az": ROUTER_AZ,
            "backoff_sequence_s": [999, 999, 999, 999],
        },
    ],
    indirect=True,
)
async def test_locality_aware_backoff_skips_sleeps(pow_2_router):
    """
    When the request router fails to route a request to a replica on the same node, and
    the same zone, it should not sleep before retrying and add additional latency.
    """
    s = pow_2_router

    # create a stub for random.sample to track the replicas that are chosen
    original_sample = random.sample
    chosen_replicas = []

    def fake_sample(seq, k):
        results = original_sample(seq, k)
        chosen_replicas.append(results)
        return results

    random.sample = fake_sample

    loop = get_or_create_event_loop()
    task = loop.create_task(s._choose_replica_for_request(fake_pending_request()))

    # Setting up 3 replicas:
    #   - r1 being same node and same zone
    #   - r2 being different node but same zone
    #   - r3 being different node and different zone
    #
    # only r3 is available to serve requests
    r1 = FakeRunningReplica("r1", node_id=ROUTER_NODE_ID, availability_zone=ROUTER_AZ)
    r2 = FakeRunningReplica(
        "r2",
        node_id="some_other_node_in_the_stratosphere",
        availability_zone=ROUTER_AZ,
    )
    r3 = FakeRunningReplica(
        "r3",
        node_id="some_other_node_in_the_stratosphere",
        availability_zone="some_other_az_in_the_solar_system",
    )
    r1.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    r2.set_queue_len_response(DEFAULT_MAX_ONGOING_REQUESTS + 1)
    r3.set_queue_len_response(0)
    s.update_replicas([r1, r2, r3])

    done, pending = await asyncio.wait([task], timeout=10)
    if len(pending) == 1:
        # r3 was not chosen after trying local node and local AZ. Which is fine.
        # clear all pending tasks
        task.cancel()
        s._routing_tasks.clear()
        s._pending_requests_to_fulfill.clear()
        s._pending_requests_to_route.clear()
    else:

        # The request will be served by r3 without added latency.
        # Since we set up the `backoff_sequence_s` to be 999s, this 10s timeout will still
        # capture the extra delay if it was added between routing loop.
        assert len(done) == 1
        assert done.pop().result() == r3

    # assert that we tried local node, followed by local AZ, followed by all replicas
    assert len(chosen_replicas) == 3
    assert set(chosen_replicas[0]) == {r1.replica_id}
    assert set(chosen_replicas[1]) == {r1.replica_id, r2.replica_id}
    # assert intersection of chosen_replicas[2] and {r1.replica_id, r2.replica_id, r3.replica_id} is not empty
    assert set(chosen_replicas[2]) & {r1.replica_id, r2.replica_id, r3.replica_id}


@pytest.mark.parametrize(
    "pow_2_router",
    [
        {"use_replica_queue_len_cache": True},
    ],
    indirect=True,
)
@pytest.mark.asyncio
async def test_select_available_replicas(pow_2_router: PowerOfTwoChoicesRequestRouter):
    """Test that the available_replicas property returns the correct replicas."""
    s = pow_2_router

    unavailable_replica = FakeRunningReplica("r1")
    available_replica_in_cache = FakeRunningReplica("r2")
    available_replica_not_in_cache = FakeRunningReplica("r3")
    all_replicas = [
        unavailable_replica,
        available_replica_in_cache,
        available_replica_not_in_cache,
    ]
    s.update_replicas(all_replicas)
    s.replica_queue_len_cache.update(
        unavailable_replica.replica_id, DEFAULT_MAX_ONGOING_REQUESTS
    )
    s.replica_queue_len_cache.update(available_replica_in_cache.replica_id, 0)

    # When no candidate replicas are provided, all replicas should be
    # considered.
    assert s.select_available_replicas() == [
        available_replica_in_cache,
        available_replica_not_in_cache,
    ]

    # When candidate replicas are provided, only those that are available
    # should be returned.
    assert s.select_available_replicas(
        [unavailable_replica, available_replica_not_in_cache]
    ) == [available_replica_not_in_cache]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pow_2_router",
    [
        {
            "az": ROUTER_AZ,
        },
    ],
    indirect=True,
)
async def test_rank_replicas_via_locality(pow_2_router: PowerOfTwoChoicesRequestRouter):
    """Test rank_replicas_via_locality returns the correct ranking."""
    s = pow_2_router

    same_node_same_zone_replica = FakeRunningReplica(
        "r1", node_id=ROUTER_NODE_ID, availability_zone=ROUTER_AZ
    )
    diff_node_same_zone_replica = FakeRunningReplica(
        "r2",
        node_id="some_other_node_in_the_stratosphere",
        availability_zone=ROUTER_AZ,
    )
    diff_node_diff_zone_replica = FakeRunningReplica(
        "r3",
        node_id="some_other_node_in_the_stratosphere",
        availability_zone="some_other_az_in_the_solar_system",
    )
    all_replicas = [
        diff_node_diff_zone_replica,
        same_node_same_zone_replica,
        diff_node_same_zone_replica,
    ]
    s.update_replicas(all_replicas)

    assert s.rank_replicas_via_locality(all_replicas) == [
        [same_node_same_zone_replica],  # same node, same zone ranked 0
        [diff_node_same_zone_replica],  # different node, same zone ranked 1
        [diff_node_diff_zone_replica],  # different node, different zone ranked 2
    ]


@pytest.mark.asyncio
async def test_rank_replicas_via_multiplex(
    pow_2_router: PowerOfTwoChoicesRequestRouter,
):
    """Test rank_replicas_via_multiplex returns the correct ranking."""
    s = pow_2_router

    replica_with_multiplexed_model = FakeRunningReplica("r1", model_ids={"m1", "m2"})
    replica_with_other_models = FakeRunningReplica("r2", model_ids={"m2", "m3"})
    replica_with_no_model = FakeRunningReplica(
        "r3",
        model_ids=set(),
    )
    all_replicas = [
        replica_with_other_models,
        replica_with_multiplexed_model,
        replica_with_no_model,
    ]
    s.update_replicas(all_replicas)

    assert s.rank_replicas_via_multiplex(
        replicas=all_replicas, multiplexed_model_id="m1"
    ) == [
        [replica_with_multiplexed_model],  # replica with the exact model ranked 0
        [replica_with_no_model],  # replica with fewer cached models ranked 1
        [replica_with_other_models],  # replica with more cached models ranked 2
    ]


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", "-s", __file__]))
