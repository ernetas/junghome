"""Shared fixtures for the Jung Home test suite."""

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.junghome.coordinator import JungHomeDataUpdateCoordinator


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "real_groups_fetch: let the test run the real _fetch_groups_from_api "
        "(pair with aioclient_mock); by default it is stubbed to avoid a socket.",
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the junghome custom integration in every test."""
    return


@pytest.fixture(autouse=True)
def mock_groups_fetch(request):
    """Keep the setup-time REST groups fetch off the network.

    ``async_setup_entry`` fetches the gateway's room groups over REST (see
    ``async_fetch_groups``) before the platforms are set up. Entity/lifecycle
    tests don't stub that call, so without this fixture they would open a real
    socket, which the Home Assistant test harness rejects. Default it to an
    empty list. Tests that assert on room -> area behaviour patch
    ``_fetch_groups_from_api`` themselves (that inner patch wins inside its
    ``with`` block), and the tests that cover the REST body opt out with
    ``@pytest.mark.real_groups_fetch``.
    """
    if request.node.get_closest_marker("real_groups_fetch") is not None:
        yield
        return
    with patch.object(
        JungHomeDataUpdateCoordinator,
        "_fetch_groups_from_api",
        AsyncMock(return_value=[]),
    ):
        yield
