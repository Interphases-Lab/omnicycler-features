"""
The OmniCycler conformance kit, for brand repos to prove they implement the standard.

In the brand repo's `tests/conftest.py`:

    pytest_plugins = ["omnicycler.features.testing.plugin"]

    @pytest.fixture
    def omnicycler_app_factory():
        return create_app

    @pytest.fixture
    def omnicycler_config():
        return BrandConfig(simulation_mode=True)

and in a test module:

    from omnicycler.features.testing import ConformanceSuite

    class TestConformance(ConformanceSuite):
        pass

Requires the `testing` and `bdf` extras.
"""

from .protocols import faulting_protocol, long_protocol, sample_for, short_protocol
from .suite import ConformanceSuite

__all__ = ["ConformanceSuite", "faulting_protocol", "long_protocol", "sample_for", "short_protocol"]
