"""API integration tests for POST /evaluate against Case A golden values."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "regression" / "fixtures" / "case_a_fixed_design.json"


@pytest.fixture(scope="module")
def client():
    os.environ["DRONE_OPTIMIZER_DATA_ROOT"] = str(REPO / "legacy" / "streamlit")
    from backend.app.main import app

    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_evaluate_case_a_matches_golden(client):
    golden = json.loads(FIXTURE.read_text())
    payload = {
        "motor_id": "NeuMotors_4610MC-207",
        "propeller_id": "APC_19x10E",
        "battery_cell_id": "fcde6dc9431046231f6f",
        "battery_topology": "6S4P",
        "esc_id": "4420b0e1b6c93b81ca45",
        "fixed_mass_kg": 0.5,
        "max_auw_kg": 10.0,
        "min_twr": 1.8,
        "frame_prop_clearance_mm": 50.0,
        "frame_structural_margin": 1.25,
    }
    r = client.post("/evaluate", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True
    assert body["motor_id"] == golden["motor_id"]
    assert body["propeller_id"] == golden["prop_id"]
    assert body["battery_id"] == golden["battery_id"]
    assert body["esc_id"] == golden["esc_id"]
    assert abs(body["auw_kg"] - golden["auw_kg"]) < 1e-6
    assert abs(body["endurance_min"] - golden["endurance_min"]) < 1e-6
    assert abs(body["max_twr"] - golden["max_twr"]) < 1e-6
    assert abs(body["hover_throttle_pct"] - golden["hover_throttle_pct"]) < 1e-6
    assert abs(body["hover_power_w"] - golden["hover_power_total_w"]) < 1e-6
    assert abs(body["frame"]["frame_mass_kg"] - golden["frame_mass_kg"]) < 1e-6
    assert int(body["frame"]["arm_tube_od_mm"]) == int(golden["frame_arm_tube_od_mm"])
    assert abs(
        body["cost"]["total_estimated_usd"]
        - golden["decision_total_estimated_cost_usd"]
    ) < 1e-6
    assert "motors" in body["cost"]["price_estimated_categories"]
    assert body["evaluation_mode"] == "exact_validated"


def test_evaluate_rejects_unknown_motor(client):
    payload = {
        "motor_id": "NOT_A_REAL_MOTOR",
        "propeller_id": "APC_19x10E",
        "battery_cell_id": "fcde6dc9431046231f6f",
        "battery_topology": "6S4P",
        "esc_id": "4420b0e1b6c93b81ca45",
        "fixed_mass_kg": 0.5,
        "max_auw_kg": 10.0,
        "min_twr": 1.8,
    }
    r = client.post("/evaluate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["rejection_reason"] == "invalid_component_selection"


def test_evaluate_commercial_pack_incomplete_is_explicit(client):
    # Packs in the current warehouse lack mass_g → clear engineering rejection.
    payload = {
        "motor_id": "NeuMotors_4610MC-207",
        "propeller_id": "APC_19x10E",
        "battery_pack_id": "e8d71fa2a91bc79d083a",
        "esc_id": "4420b0e1b6c93b81ca45",
        "fixed_mass_kg": 0.5,
        "max_auw_kg": 10.0,
        "min_twr": 1.8,
    }
    r = client.post("/evaluate", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["rejection_reason"] == "commercial_pack_incomplete"
