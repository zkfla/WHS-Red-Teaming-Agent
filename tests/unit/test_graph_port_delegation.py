"""Graph optional-port checks stay valid across Python 3.10–3.14 wrappers."""

from types import SimpleNamespace

import pytest

from redteam.attack_graph.service import AttackGraphService
from redteam.attack_graph.stores.base import (
    StoreCapabilityError,
    require_listable,
    require_mutable,
    require_receipted,
)
from redteam.attack_graph.stores.memory import InMemoryAttackGraphStore
from redteam.orchestration.planning_admission import PlanningAdmission
from redteam.orchestration.planning_graph import PlanningGraphStore
from tests.unit.test_orchestration_coordination import world


class DelegatedStore:
    def __init__(self, store):
        self.store = store

    def __getattr__(self, name):
        return getattr(self.store, name)


PORTS = (
    (lambda store: require_listable(store, "observe"), ("list_nodes", "list_edges")),
    (lambda store: require_mutable(store, "retract"), ("remove_node", "remove_edge")),
    (require_receipted, (
        "prepare_graph_mutation", "apply_graph_mutation", "read_graph_mutation_receipt",
    )),
)


@pytest.mark.parametrize("require,names", PORTS)
def test_delegation_preserves_the_wrapper_not_its_inner_store(require, names):
    store = DelegatedStore(InMemoryAttackGraphStore())
    assert require(store) is store


@pytest.mark.parametrize("require,names", PORTS)
@pytest.mark.parametrize("missing", [True, False])
def test_partial_or_noncallable_port_is_never_advertised(require, names, missing):
    methods = {name: lambda: None for name in names}
    if missing:
        del methods[names[-1]]
    else:
        methods[names[-1]] = None
    with pytest.raises(StoreCapabilityError):
        require(DelegatedStore(SimpleNamespace(**methods)))


async def test_planning_wrapper_can_be_observed_and_reported_without_bypassing_it(tmp_path):
    w = await world(tmp_path, "memory")
    inner = InMemoryAttackGraphStore()
    wrapped = PlanningGraphStore(inner, PlanningAdmission(w.coordinator))
    assert require_listable(wrapped, "report") is wrapped
    service = AttackGraphService(wrapped)
    assert await service.view().counts() == {"nodes": 0, "edges": 0}
    assert (await service.integrity_report()).is_clean
