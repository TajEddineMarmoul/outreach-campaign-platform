"""Shared test isolation.

The hosted MCP transport is built during application startup, and its session
manager can only be started once per instance. Tests start the application many
times in one process, so the mount and the in-process client are reset between
tests.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Mount

import pytest


def _drop_hosted_transport(app) -> None:
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            isinstance(route, Mount)
            and route.path == "/"
            and isinstance(getattr(route, "app", None), Starlette)
        )
    ]


@pytest.fixture(autouse=True)
def _reset_hosted_mcp_state():
    from api.main import app
    from outreach_mcp import server as mcp_server

    def reset() -> None:
        mcp_server._hosted_client.cache_clear()
        _drop_hosted_transport(app)

    reset()
    try:
        yield
    finally:
        reset()
