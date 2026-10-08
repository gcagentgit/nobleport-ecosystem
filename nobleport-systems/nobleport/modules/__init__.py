"""All 10 NoblePort modules."""

from . import (
    building_code,
    cyborg,
    esign,
    estimator,
    permitstream,
    project_manager,
    revenue,
    stephanie,
    structural,
    web3,
)

BUILDERS = [
    stephanie.build,
    permitstream.build,
    estimator.build,
    project_manager.build,
    structural.build,
    building_code.build,
    cyborg.build,
    esign.build,
    revenue.build,
    web3.build,
]
