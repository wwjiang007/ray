import ray.dashboard.utils as dashboard_utils
import ray.dashboard.optional_utils as optional_utils
from ray.dashboard.modules.healthz.utils import HealthChecker
from aiohttp.web import Request, Response, HTTPServiceUnavailable
import grpc

routes = optional_utils.ClassMethodRouteTable


class HealthzAgent(dashboard_utils.DashboardAgentModule):
    def __init__(self, dashboard_agent):
        super().__init__(dashboard_agent)
        self._health_checker = HealthChecker(
            dashboard_agent.gcs_aio_client,
            f"{dashboard_agent.ip}:{dashboard_agent.node_manager_port}",
        )

    @routes.get("/api/local_raylet_healthz/")
    async def health_check(self, req: Request) -> Response:
        try:
            alive = await self._health_checker.check_local_raylet_liveness()
            if alive is False:
                return HTTPServiceUnavailable(reason="Local Raylet failed")
        except grpc.RpcError as e:
            # We only consider the error other than GCS unreachable as raylet failure
            # to avoid false positive.
            # In case of GCS failed, Raylet will crash eventually if GCS is not back
            # within a given time and the check will fail since agent can't live
            # without a local raylet.
            if e.code() not in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.UNKNOWN,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            ):
                return HTTPServiceUnavailable(reason=e.message())

        return Response(
            text="success",
            content_type="application/text",
        )
