from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

from hushine_strategy.replay.spot_filters import evaluate_spot_filter_vector


FIXTURE = Path(__file__).parents[1] / "fixtures" / "spot_filter_contract_v1.json"
CORE_FIXTURE = (
    Path(__file__).parents[3]
    / "core-service"
    / "internal"
    / "order"
    / "risk"
    / "testdata"
    / "spot_filter_contract_v1.json"
)


def test_spot_filter_fixture_is_byte_identical_to_core_generator_output():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == hashlib.sha256(
        CORE_FIXTURE.read_bytes()
    ).hexdigest()


def test_every_core_spot_filter_vector_has_the_same_stable_result(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )
    payload = json.loads(FIXTURE.read_text())

    assert payload["schema_version"] == "spot_filter_contract_v1"
    assert len(payload["cases"]) >= 20
    for case in payload["cases"]:
        assert evaluate_spot_filter_vector(case) == case["expected_code"], case["name"]
