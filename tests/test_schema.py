"""Unit / schema layer: extracted objects conform to the versioned schema; fail loud on missing mandatory fields."""
from datetime import date

import pytest
from pydantic import ValidationError

from pqm_agent.models import Case, FailureDescription, ProductIdentity, Severity, SCHEMA_VERSION


def _case(**overrides):
    base = dict(customer="BMW", plant="Plant A", owner="M. Ortega",
                product=ProductIdentity(part_number="123", part_name="EGR cooler"),
                failure=FailureDescription(failure_mode="leak"), severity=Severity.HIGH,
                detection_date=date(2026, 8, 27), opened_date=date(2026, 8, 28), customer_due_date=date(2026, 9, 26))
    base.update(overrides)
    return Case(**base)


def test_case_has_canonical_id_and_schema_version():
    c = _case()
    assert c.case_id.startswith("CASE-")
    assert c.schema_version == SCHEMA_VERSION


def test_missing_mandatory_field_fails_loud():
    with pytest.raises(ValidationError):
        _case(customer=None)


def test_empty_identifier_rejected():
    with pytest.raises(ValidationError):
        _case(plant="   ")


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        _case(unexpected="x")
