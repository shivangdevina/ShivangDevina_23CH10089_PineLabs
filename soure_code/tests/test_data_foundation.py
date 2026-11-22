from copy import deepcopy
from datetime import timedelta
from importlib.resources import files

import pytest

from payments_analyst.cli import main
from payments_analyst.data import DatasetConfig, basis_points, generate
from payments_analyst.validation import DataQualityError, validate


@pytest.fixture(scope="module")
def small():
    return generate(DatasetConfig(orders_per_day=10))


@pytest.fixture(scope="module")
def full():
    return generate()


def test_same_configuration_reproduces_entire_dataset(small):
    assert small.fingerprint() == generate(DatasetConfig(orders_per_day=10)).fingerprint()


def test_configuration_changes_dataset_identity(small):
    assert small.version["id"] != generate(DatasetConfig(seed=43, orders_per_day=10)).version["id"]
    assert small.version["id"] != generate(DatasetConfig(orders_per_day=11)).version["id"]


def test_full_dataset_is_valid_and_contains_retries_and_pending(full):
    validate(full)
    orders = full.tables["orders"]
    assert len(orders) == 63 * 320
    assert len(full.tables["payment_attempts"]) > len(orders)
    assert any(o["final_status"] == "PENDING" for o in orders)
    assert full.version["window_end"] - full.version["current_start"] == timedelta(days=7)
    assert full.version["history_start"].utcoffset() == timedelta(hours=5, minutes=30)


def test_money_rounds_half_up_without_float_arithmetic():
    assert basis_points(10_000, 100) == 100
    assert basis_points(50, 100) == 1  # half a paise rounds up
    assert basis_points(49, 100) == 0
    assert basis_points(1_000_000_001, 250) == 25_000_000
    with pytest.raises(ValueError):
        basis_points(-1, 100)


def test_duplicate_callback_record_rejected(small):
    bad = deepcopy(small)
    bad.tables["payment_attempts"].append(deepcopy(bad.tables["payment_attempts"][0]))
    with pytest.raises(DataQualityError, match="Duplicate id"):
        validate(bad)


def test_missing_cost_rule_rejected(small):
    bad = deepcopy(small)
    bad.tables["cost_rules"].clear()
    with pytest.raises(DataQualityError, match="Missing cost rule"):
        validate(bad)


def test_cost_rule_must_be_effective(small):
    bad = deepcopy(small)
    bad.tables["cost_rules"][0]["effective_to"] = bad.version["history_start"]
    with pytest.raises(DataQualityError, match="not effective"):
        validate(bad)


def test_ledger_tampering_rejected(small):
    bad = deepcopy(small)
    bad.tables["financial_events"][0]["amount_paise"] += 1
    with pytest.raises(DataQualityError, match="does not reconcile"):
        validate(bad)


def test_missing_attempt_fee_rejected(small):
    bad = deepcopy(small)
    bad.tables["financial_events"].pop()
    with pytest.raises(DataQualityError, match="does not reconcile"):
        validate(bad)


def test_cross_order_ledger_reference_rejected(small):
    bad = deepcopy(small)
    event = bad.tables["financial_events"][0]
    event["order_id"] = next(o["id"] for o in bad.tables["orders"] if o["id"] != event["order_id"])
    with pytest.raises(DataQualityError, match="Ledger/order mismatch"):
        validate(bad)


def test_successful_order_cannot_still_be_pending(small):
    bad = deepcopy(small)
    order = next(o for o in bad.tables["orders"] if o["final_status"] == "SUCCESS")
    order["final_status"] = "PENDING"
    with pytest.raises(DataQualityError, match="Order/final attempt mismatch"):
        validate(bad)


def test_each_failed_attempt_is_charged(small):
    events = small.tables["financial_events"]
    failures = [a for a in small.tables["payment_attempts"] if a["status"] == "FAILED"]
    costs = {e["attempt_id"]: e for e in events if e["event_type"] == "PROCESSING_COST"}
    assert failures
    for attempt in failures:
        assert costs[attempt["id"]]["amount_paise"] == -12


def test_fee_revenue_is_not_merchant_payment_value(small):
    fees = sum(e["amount_paise"] for e in small.tables["financial_events"] if e["event_type"] == "MERCHANT_FEE")
    successful_value = sum(o["amount_paise"] for o in small.tables["orders"] if o["final_status"] == "SUCCESS")
    assert 0 < fees < successful_value


def test_synthetic_decline_and_mix_shift_are_present(full):
    merchants = {m["id"]: m for m in full.tables["merchants"]}
    current_start = full.version["current_start"]

    def cohort(bank, method, current, category=None):
        return [o for o in full.tables["orders"]
                if o["initial_bank"] == bank and o["initial_payment_method"] == method
                and (o["created_at"] >= current_start) == current
                and o["final_status"] != "PENDING"
                and (category is None or merchants[o["merchant_id"]]["category"] == category)]

    def rate(rows):
        return sum(o["final_status"] == "SUCCESS" for o in rows) / len(rows)

    assert rate(cohort("ATLAS", "UPI", False)) - rate(cohort("ATLAS", "UPI", True)) > 0.08
    assert rate(cohort("CEDAR", "UPI", False)) - rate(cohort("CEDAR", "UPI", True)) > 0.04
    for category in ("RETAIL", "DIGITAL"):
        assert abs(rate(cohort("CEDAR", "UPI", False, category))
                   - rate(cohort("CEDAR", "UPI", True, category))) < 0.10


def test_expensive_route_loses_money_even_when_successful(full):
    costs = {r["id"]: r for r in full.tables["cost_rules"]}
    merchants = {m["id"]: m for m in full.tables["merchants"]}
    orders = {o["id"]: o for o in full.tables["orders"]}
    matches = [a for a in full.tables["payment_attempts"] if a["bank"] == "DELTA"
               and a["payment_method"] == "CARD" and a["status"] == "SUCCESS"]
    assert matches
    for attempt in matches:
        order = orders[attempt["order_id"]]
        merchant, cost = merchants[order["merchant_id"]], costs[attempt["cost_rule_id"]]
        fee = basis_points(order["amount_paise"], merchant["fee_bps"]) + merchant["fee_fixed_paise"]
        processing = basis_points(order["amount_paise"], cost["success_bps"]) + cost["success_fixed_paise"]
        assert fee - processing < 0


def test_ground_truth_is_separate_and_healthy_control_has_no_injected_incidents(small):
    assert len(small.ground_truth) == 4
    assert "ground_truth" not in small.tables
    healthy = generate(DatasetConfig(orders_per_day=10, scenario="healthy"))
    validate(healthy)
    assert healthy.ground_truth == []


def test_preview_runs_without_database_credentials(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    assert main(["preview", "--orders-per-day", "10"]) == 0
    assert '"synthetic": true' in capsys.readouterr().out


def test_schema_parses_as_postgresql():
    from pglast import parse_sql
    for migration in files("payments_analyst").joinpath("migrations").iterdir():
        if migration.name.endswith(".sql"):
            assert parse_sql(migration.read_text(encoding="utf-8"))


@pytest.mark.parametrize("config", [
    {"history_days": 13}, {"current_days": 8}, {"orders_per_day": 0}, {"scenario": "unknown"}
])
def test_invalid_generation_configuration_rejected(config):
    with pytest.raises(ValueError):
        DatasetConfig(**config)
