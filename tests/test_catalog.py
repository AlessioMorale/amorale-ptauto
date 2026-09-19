"""The catalog adapter: models, ports and cables."""

from __future__ import annotations

import pytest

from ptauto.catalog import (
    infer_cable_for,
    model_info,
    normalise_port,
    resolve_cable,
    suggest_models,
)
from ptauto.errors import SpecError


@pytest.mark.parametrize(
    "written,canonical",
    [
        ("g0/0", "GigabitEthernet0/0"),
        ("Gi0/1", "GigabitEthernet0/1"),
        ("gig0/2", "GigabitEthernet0/2"),
        ("GigabitEthernet0/0", "GigabitEthernet0/0"),
        ("fa0/1", "FastEthernet0/1"),
        ("f0", "FastEthernet0"),
        ("Se0/0/0", "Serial0/0/0"),
        ("vlan10", "Vlan10"),
    ],
)
def test_port_shorthands_expand(written, canonical):
    assert normalise_port(written) == canonical


def test_a_name_that_is_not_a_port_is_left_alone():
    assert normalise_port("Console") == "Console"


def test_models_carry_their_ports_and_category():
    router = model_info("2911")
    assert router.category == "router" and router.is_ios and not router.is_host
    assert router.ports == (
        "GigabitEthernet0/0",
        "GigabitEthernet0/1",
        "GigabitEthernet0/2",
    )
    assert model_info("PC-PT").is_host
    assert model_info("Server-PT").category == "server"


def test_an_unknown_model_suggests_near_misses():
    with pytest.raises(SpecError):
        model_info("296024TT")
    assert "2960-24TT" in suggest_models("2960")


@pytest.mark.parametrize(
    "a,b,cable",
    [("router", "switch", "straight"), ("switch", "pc", "straight"), ("switch", "switch", "cross")],
)
def test_cables_are_inferred_from_categories(a, b, cable):
    assert infer_cable_for(a, b) == cable


def test_cable_aliases_resolve_and_typos_are_rejected():
    assert resolve_cable("crossover") == "cross"
    assert resolve_cable(None) is None
    with pytest.raises(SpecError, match="Unknown cable type"):
        resolve_cable("cat6")
