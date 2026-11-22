from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
import json

import pytest

from payments_analyst.analytics import analyse, scenario_from_analysis
from payments_analyst.contracts import AnalysisBlocked, AnalysisRequest, EngineSettings, ScenarioRequest
from payments_analyst.data import DatasetConfig, generate
from payments_analyst.finance import RetryEconomics, simulate
from payments_analyst.preview import snapshot_from_generated


@pytest.fixture(scope="module")
def incident_snapshot():
    return snapshot_from_generated(generate())


@pytest.fixture(scope="module")
def report(incident_snapshot):
    return analyse(incident_snapshot, AnalysisRequest(dataset_id=incident_snapshot.version["id"]))


def by_id(report, identifier):
    return next(s for s in report["segments"] if s["segment_id"] == identifier)


def test_financial_engine_matches_hand_calculation_and_counts_failed_retry_costs():
    economics = RetryEconomics(100, 1_000_000, 10_000, 3_000, 1_000, 500)
    result = simulate(economics, 20, Decimal("0.5"), 100)
    assert result["additional_successful_orders"] == 10
    assert result["additional_payment_value_paise"] == 100_000
    assert result["additional_merchant_fees_paise"] == 1_000
    assert result["successful_retry_cost_paise"] == 300
    assert result["failed_retry_cost_paise"] == 900
    assert result["additional_loss_provision_paise"] == 50
    assert result["incremental_net_contribution_paise"] == -350
    assert result["break_even_recovery_fraction"] == pytest.approx(11 / 15)


def test_zero_recovery_is_not_zero_cost_and_full_recovery_can_break_even():
    economics = RetryEconomics(100, 1_000_000, 10_000, 3_000, 1_000, 500)
    assert simulate(economics, 20, Decimal(0), 100)["incremental_net_contribution_paise"] == -1_100
    assert simulate(economics, 20, Decimal(1), 100)["incremental_net_contribution_paise"] == 400


def test_recovery_is_capped_at_eligible_orders():
    economics = RetryEconomics(10, 100_000, 1_000, 300, 100, 50)
    result = simulate(economics, 100)
    assert result["additional_successful_orders"] == 10
    assert result["failed_retry_cost_paise"] == 0


@pytest.mark.parametrize("fraction", [Decimal("NaN"), Decimal("Infinity"), Decimal("-0.1"), Decimal("1.1")])
def test_invalid_scenario_fraction_rejected(fraction):
    with pytest.raises(ValueError):
        simulate(RetryEconomics(10, 100_000, 1_000, 300, 100, 50), 5, fraction)


def test_actual_synthetic_incident_is_prioritised_without_ground_truth(report):
    atlas = by_id(report, "ATLAS__UPI")
    assert atlas["rank"] == 1
    assert atlas["status"] == "ACTIONABLE"
    assert atlas["net_impact_interval_paise"][0] > 0
    assert report["source"] == "generated_in_memory_preview"
    assert report["query_evidence"][0]["execution"] == "NOT_EXECUTED"
    json.dumps(report, allow_nan=False)


def test_mix_shift_is_not_ranked_as_a_bank_performance_problem(report):
    cedar = by_id(report, "CEDAR__UPI")
    raw_gap = 100 * (cedar["raw_historical_success_rate"] - cedar["current_success_rate"])
    assert raw_gap > 5
    assert abs(cedar["gap_percentage_points"]) < 3
    assert cedar["status"] == "NO_CLEAR_DECLINE"
    assert cedar["rank"] is None


def test_expensive_recovery_and_tiny_segment_are_not_prioritised(report):
    assert by_id(report, "DELTA__CARD")["status"] == "NOT_PROFITABLE"
    assert by_id(report, "DELTA__CARD")["rank"] is None
    assert by_id(report, "BREEZE__CARD")["status"] == "INSUFFICIENT_EVIDENCE"
    assert by_id(report, "BREEZE__CARD")["rank"] is None


def test_healthy_control_has_no_ranked_opportunity():
    snapshot = snapshot_from_generated(generate(DatasetConfig(scenario="healthy")))
    result = analyse(snapshot, AnalysisRequest(dataset_id=snapshot.version["id"]))
    assert not any(s["rank"] for s in result["segments"])


def test_ranking_and_simulator_use_identical_financial_results(report):
    atlas = by_id(report, "ATLAS__UPI")
    result = scenario_from_analysis(report, ScenarioRequest(segment_id="ATLAS__UPI", recovery_fraction=1))
    assert result["result"] == atlas["reference_scenario"]
    assert result["curve"][-1]["incremental_net_contribution_paise"] == atlas["reference_scenario"]["incremental_net_contribution_paise"]
    assert result["curve"][0]["incremental_net_contribution_paise"] < 0


def test_intervention_cost_is_subtracted_exactly(report):
    zero = scenario_from_analysis(report, ScenarioRequest(segment_id="ATLAS__UPI", recovery_fraction="0.5"))
    paid = scenario_from_analysis(report, ScenarioRequest(segment_id="ATLAS__UPI", recovery_fraction="0.5", intervention_cost_paise=1234))
    assert zero["result"]["incremental_net_contribution_paise"] - paid["result"]["incremental_net_contribution_paise"] == 1234


def test_estimates_remain_reproducible(incident_snapshot, report):
    assert analyse(incident_snapshot, AnalysisRequest(dataset_id=incident_snapshot.version["id"])) == report


@pytest.mark.parametrize("field", ["order_count", "ledger_net_paise"])
def test_source_reconciliation_failure_blocks_analysis(incident_snapshot, field):
    bad = deepcopy(incident_snapshot)
    bad.diagnostics[field] += 1
    with pytest.raises(AnalysisBlocked):
        analyse(bad, AnalysisRequest(dataset_id=bad.version["id"]))


def test_duplicate_aggregate_blocks_analysis(incident_snapshot):
    bad = deepcopy(incident_snapshot)
    bad.daily.append(deepcopy(bad.daily[0]))
    with pytest.raises(AnalysisBlocked, match="Duplicate"):
        analyse(bad, AnalysisRequest(dataset_id=bad.version["id"]))


def test_missing_or_overlapping_pricing_withholds_financial_ranking(incident_snapshot):
    bad = deepcopy(incident_snapshot)
    start = bad.version["current_start"].date()
    row = next(r for r in bad.daily if r["bank"] == "ATLAS" and r["payment_method"] == "UPI"
               and r["business_date"] >= start and r["eligible_orders"] > 0)
    row["unpriced_orders"] = 1
    result = analyse(bad, AnalysisRequest(dataset_id=bad.version["id"]))
    assert by_id(result, "ATLAS__UPI")["status"] == "UNPRICED"
    with pytest.raises(AnalysisBlocked):
        scenario_from_analysis(result, ScenarioRequest(segment_id="ATLAS__UPI"))


def test_pending_orders_are_excluded_and_surfaced(report):
    assert sum(s["pending_orders"] for s in report["segments"]) == 3
    assert sum(s["current_completed_orders"] for s in report["segments"]) == 7 * 320 - 3
    assert any("Pending orders" in reason for s in report["segments"] for reason in s["reasons"])


def test_unsupported_simulation_is_blocked(report):
    with pytest.raises(AnalysisBlocked):
        scenario_from_analysis(report, ScenarioRequest(segment_id="BREEZE__CARD"))


def test_price_cap_does_not_silently_imply_zero_uncertainty(report):
    segment = by_id(report, "ATLAS__UPI")
    if segment["bootstrap_cap_binding_fraction"] > 0.5:
        assert any("does not imply certain recovery" in r for r in segment["reasons"])


def test_too_few_orders_withhold_conclusions():
    snapshot = snapshot_from_generated(generate(DatasetConfig(orders_per_day=10)))
    result = analyse(snapshot, AnalysisRequest(dataset_id=snapshot.version["id"]))
    assert all(s["status"] == "INSUFFICIENT_EVIDENCE" for s in result["segments"])


def test_known_answer_mix_adjustment_removes_exact_composition_effect(incident_snapshot):
    snapshot = deepcopy(incident_snapshot)
    snapshot.daily = []
    first = snapshot.version["history_start"].date()
    current = snapshot.version["current_start"].date()
    for offset in range(63):
        day = first + timedelta(days=offset)
        for category in ("RETAIL", "DIGITAL"):
            # History mix: 90% RETAIL. Current mix: 90% DIGITAL.
            n = 90 if ((day < current) == (category == "RETAIL")) else 10
            successes = n * (9 if category == "RETAIL" else 5) // 10
            failed = n - successes
            snapshot.daily.append({"bank": "ATLAS", "payment_method": "UPI", "business_date": day,
                "merchant_category": category, "amount_band": "UNDER_500", "total_orders": n,
                "completed_orders": n, "successful_orders": successes, "pending_orders": 0,
                "eligible_orders": failed, "unpriced_orders": 0,
                "eligible_payment_value_paise": failed * 10_000, "retry_fee_paise": failed * 100,
                "retry_success_cost_paise": failed * 20, "retry_failure_cost_paise": failed * 10,
                "retry_loss_provision_paise": failed, "net_contribution_paise": 0})
    result = analyse(snapshot, AnalysisRequest(dataset_id=snapshot.version["id"]))
    segment = result["segments"][0]
    assert segment["raw_historical_success_rate"] == pytest.approx(0.86)
    assert segment["current_success_rate"] == pytest.approx(0.54)
    assert segment["benchmark_success_rate"] == pytest.approx(0.54)
    assert segment["gap_percentage_points"] == pytest.approx(0)
    assert segment["rank"] is None


def test_approved_query_templates_parse_as_postgresql():
    import re
    from pglast import parse_sql
    from payments_analyst import queries
    for name in ("DAILY_SQL", "VERSION_SQL", "DIAGNOSTICS_SQL", "LIST_DATASETS_SQL"):
        text = re.sub(r"%\([a-z_]+\)s", "NULL", getattr(queries, name))
        assert len(parse_sql(text)) == 1
