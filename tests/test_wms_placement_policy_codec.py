from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from modules.wms.physical_placement import (
    Box,
    Corridor,
    LoadRule,
    Opening,
    PlacementInputError,
    Policy,
    Route,
    _canonical_payload,
    _json,
    _sha,
)
from modules.wms.placement_policy_codec import decode_placement_work_policy


def _uuid(number: int) -> str:
    return str(UUID(int=number))


def _record() -> tuple[dict[str, object], Policy]:
    binding = {"binding_format": "opaque-test-v1", "version": 1}
    route = Route(
        source_id=11,
        destination_id=12,
        source_profile_id=_uuid(30),
        source_digest="a" * 64,
        destination_profile_id=_uuid(31),
        destination_digest="b" * 64,
        source_mapping_revision=1,
        destination_mapping_revision=1,
        resource_ids=(_uuid(32), _uuid(33)),
        phase_resource_paths=((_uuid(32),), (_uuid(33),)),
        slot_groups=(("B-01", "B-02"),),
        openings=(Opening("x", "y", Decimal("120"), Decimal("80"), Decimal("1")),),
        corridors=(
            Corridor(
                Box(
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("0"),
                    Decimal("10"),
                    Decimal("10"),
                    Decimal("10"),
                ),
                "frame-12",
                3,
            ),
        ),
        access_digest="c" * 64,
        evidence="route evidence",
    )
    policy = Policy(
        id=_uuid(0xABC),
        organization_id=7,
        warehouse_id=9,
        scope_code="warehouse",
        revision=1,
        access_method="manual",
        operator_subs=("operator-1",),
        routes=(route,),
        equipment_resource_id=None,
        equipment_profile_digest=None,
        equipment_state="unknown",
        manual_gross_limit_kg=Decimal("20"),
        manual_height_limit_mm=Decimal("500"),
        equipment_gross_limit_kg=None,
        equipment_height_limit_mm=None,
        load_rules=(
            LoadRule(
                _uuid(33),
                "required",
                Decimal("500"),
                _uuid(34),
                "d" * 64,
            ),
        ),
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
        source_ref="policy source",
        source_revision="rev-1",
        evidence="approval evidence",
        approved_by="operator-1",
        approved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        approval_operation_id=_uuid(2),
        server_binding_digest=_sha(_json(binding)),
        canonical_payload="",
        digest="",
    )
    payload = _canonical_payload(policy)
    policy = replace(policy, canonical_payload=payload, digest=_sha(payload))
    record: dict[str, object] = {
        "id": policy.id,
        "organization_id": policy.organization_id,
        "warehouse_id": policy.warehouse_id,
        "scope_code": policy.scope_code,
        "revision": policy.revision,
        "access_method": policy.access_method,
        "valid_from": policy.valid_from,
        "valid_until": policy.valid_until,
        "policy": {
            "operator_subs": ["operator-1"],
            "routes": [
                {
                    "source_id": 11,
                    "destination_id": 12,
                    "source_profile_id": _uuid(30),
                    "source_digest": "a" * 64,
                    "destination_profile_id": _uuid(31),
                    "destination_digest": "b" * 64,
                    "source_mapping_revision": 1,
                    "destination_mapping_revision": 1,
                    "resource_ids": [_uuid(32), _uuid(33)],
                    "phase_resource_paths": [[_uuid(32)], [_uuid(33)]],
                    "slot_groups": [["B-01", "B-02"]],
                    "openings": [
                        {
                            "width_axis": "x",
                            "height_axis": "y",
                            "width_mm": "120.000000",
                            "height_mm": "80.000000",
                            "clearance_mm": "1.000000",
                        }
                    ],
                    "corridors": [
                        {
                            "box": {
                                "x": "0.000000",
                                "y": "0.000000",
                                "z": "0.000000",
                                "w": "10.000000",
                                "h": "10.000000",
                                "d": "10.000000",
                            },
                            "frame_id": "frame-12",
                            "older_fefo_rank": 3,
                        }
                    ],
                    "access_digest": "c" * 64,
                    "evidence": "route evidence",
                }
            ],
            "equipment_resource_id": None,
            "equipment_profile_digest": None,
            "equipment_state": "unknown",
            "manual_gross_limit_kg": "20.000000",
            "manual_height_limit_mm": "500.000000",
            "equipment_gross_limit_kg": None,
            "equipment_height_limit_mm": None,
            "load_rules": [
                {
                    "resource_id": _uuid(33),
                    "state": "required",
                    "gross_limit_kg": "500.000000",
                    "source_profile_id": _uuid(34),
                    "source_profile_digest": "d" * 64,
                }
            ],
        },
        "server_binding": binding,
        "canonical_payload": policy.canonical_payload,
        "digest": policy.digest,
        "source_ref": policy.source_ref,
        "source_revision": policy.source_revision,
        "evidence": policy.evidence,
        "approved_by": policy.approved_by,
        "approved_at": policy.approved_at,
        "approval_operation_id": policy.approval_operation_id,
    }
    return record, policy


def test_native_nested_policy_decodes_byte_exactly_without_mutating_record() -> None:
    record, expected = _record()
    before = deepcopy(record)

    decoded = decode_placement_work_policy(record)

    assert decoded == expected
    assert decoded.canonical_payload == expected.canonical_payload
    assert decoded.digest == expected.digest
    assert decoded.server_binding_digest == expected.server_binding_digest
    assert '"valid_from":"2026-01-01T00:00:00.000000Z"' in decoded.canonical_payload
    assert record == before
    assert record["canonical_payload"] == before["canonical_payload"]
    assert record["digest"] == before["digest"]


def _unknown_row_key(record: dict[str, object]) -> None:
    record["extra"] = "forbidden"


def _missing_policy_key(record: dict[str, object]) -> None:
    del record["policy"]["load_rules"]


def _unknown_route_key(record: dict[str, object]) -> None:
    record["policy"]["routes"][0]["unreviewed"] = True


def _header_body_mismatch(record: dict[str, object]) -> None:
    record["revision"] = 2


def _canonical_body_mismatch(record: dict[str, object]) -> None:
    record["policy"]["operator_subs"] = ["another-operator"]


def _digest_mismatch(record: dict[str, object]) -> None:
    record["digest"] = "0" * 64


def _float_decimal(record: dict[str, object]) -> None:
    record["policy"]["manual_gross_limit_kg"] = 20.0


def _bool_integer(record: dict[str, object]) -> None:
    record["policy"]["routes"][0]["source_id"] = True


def _decimal_overprecision(record: dict[str, object]) -> None:
    record["policy"]["manual_gross_limit_kg"] = "20.0000001"


def _decimal_overflow(record: dict[str, object]) -> None:
    record["policy"]["manual_gross_limit_kg"] = "100000000000000.000000"


def _duplicate_operator(record: dict[str, object]) -> None:
    record["policy"]["operator_subs"].append("operator-1")


def _duplicate_resource(record: dict[str, object]) -> None:
    record["policy"]["routes"][0]["resource_ids"].append(_uuid(32))


def _duplicate_phase_resource(record: dict[str, object]) -> None:
    record["policy"]["routes"][0]["phase_resource_paths"][0].append(_uuid(32))


def _duplicate_slot_vector(record: dict[str, object]) -> None:
    record["policy"]["routes"][0]["slot_groups"][0].append("B-01")


def _duplicate_load_rule(record: dict[str, object]) -> None:
    record["policy"]["load_rules"].append(deepcopy(record["policy"]["load_rules"][0]))


def _naive_row_datetime(record: dict[str, object]) -> None:
    record["valid_from"] = datetime(2026, 1, 1)


def _noncanonical_uuid(record: dict[str, object]) -> None:
    original = record["id"]
    record["id"] = original.upper()
    assert record["id"] != original


def _binding_float(record: dict[str, object]) -> None:
    record["server_binding"]["coordinates"] = [0.5]


def _binding_nonstring_key(record: dict[str, object]) -> None:
    record["server_binding"][1] = "untrusted"


@pytest.mark.parametrize(
    "mutate",
    [
        _unknown_row_key,
        _missing_policy_key,
        _unknown_route_key,
        _header_body_mismatch,
        _canonical_body_mismatch,
        _digest_mismatch,
        _float_decimal,
        _bool_integer,
        _decimal_overprecision,
        _decimal_overflow,
        _duplicate_operator,
        _duplicate_resource,
        _duplicate_phase_resource,
        _duplicate_slot_vector,
        _duplicate_load_rule,
        _naive_row_datetime,
        _noncanonical_uuid,
        _binding_float,
        _binding_nonstring_key,
    ],
)
def test_invalid_record_is_rejected_without_mutating_native_signature_or_digest(mutate) -> None:
    record, _expected = _record()
    mutate(record)
    before = deepcopy(record)

    with pytest.raises(PlacementInputError):
        decode_placement_work_policy(record)

    assert record == before
    assert record["canonical_payload"] == before["canonical_payload"]
    assert record["digest"] == before["digest"]
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("id", _uuid(99)),
        ("organization_id", 8),
        ("warehouse_id", 10),
        ("scope_code", "other-scope"),
        ("revision", 2),
        ("access_method", "equipment"),
        ("valid_from", datetime(2026, 1, 2, tzinfo=timezone.utc)),
        ("valid_until", datetime(2027, 1, 2, tzinfo=timezone.utc)),
        ("source_ref", "other source"),
        ("source_revision", "rev-2"),
        ("evidence", "other evidence"),
        ("approved_by", "other approver"),
        ("approved_at", datetime(2026, 1, 3, tzinfo=timezone.utc)),
        ("approval_operation_id", _uuid(99)),
    ],
)
def test_every_header_column_is_bound_to_canonical_policy_body(field, replacement) -> None:
    record, _expected = _record()
    record[field] = replacement
    before = deepcopy(record)

    with pytest.raises(PlacementInputError):
        decode_placement_work_policy(record)

    assert record == before
    assert record["canonical_payload"] == before["canonical_payload"]
    assert record["digest"] == before["digest"]

@pytest.mark.parametrize("value", ["0.000000", "-0.000000", "1e100000000", "1e-100000000"])
def test_decimal_bounds_precede_formatting_and_only_native_zero_is_canonical(value) -> None:
    record, expected = _record()
    box = record["policy"]["routes"][0]["corridors"][0]["box"]
    assert box["x"] == "0.000000"
    box["x"] = value
    before = deepcopy(record)
    if value == "0.000000":
        assert decode_placement_work_policy(record) == expected
    else:
        with pytest.raises(PlacementInputError):
            decode_placement_work_policy(record)
    assert record == before


def test_exact_decimal_policy_amount_is_bound_to_native_warehouse_record() -> None:
    record, expected = _record()
    exact_amount = Decimal("20.000001")
    expected = replace(
        expected,
        manual_gross_limit_kg=exact_amount,
        canonical_payload="",
        digest="",
    )
    canonical_payload = _canonical_payload(expected)
    expected = replace(
        expected,
        canonical_payload=canonical_payload,
        digest=_sha(canonical_payload),
    )
    record["policy"]["manual_gross_limit_kg"] = "20.000001"
    record["canonical_payload"] = expected.canonical_payload
    record["digest"] = expected.digest
    before = deepcopy(record)

    decoded = decode_placement_work_policy(record)

    assert decoded == expected
    assert decoded.manual_gross_limit_kg == exact_amount
    assert record == before

    tampered = deepcopy(record)
    tampered["policy"]["manual_gross_limit_kg"] = "20.000002"
    tampered_before = deepcopy(tampered)
    with pytest.raises(PlacementInputError):
        decode_placement_work_policy(tampered)
    assert tampered == tampered_before
