from copy import deepcopy

import pytest

from payments_analyst.presentation import present, present_query


def query_report(rows, columns=None, intent="overview"):
    return {"sql_result":{"rows":rows,"columns":columns or list(rows[0])},
            "planning":{"plan":{"intent":intent}}}


def assert_separated(answer):
    assert answer["facts"] and answer["explanations"] and answer["recommendations"]
    assert answer["hypotheses"] == []
    ids={fact["id"] for fact in answer["facts"]}
    assert all(item["kind"]=="observed" for item in answer["facts"])
    assert all(item["kind"]=="explanation" for item in answer["explanations"])
    assert all(item["kind"]=="recommendation" for item in answer["recommendations"])
    for item in answer["explanations"]+answer["recommendations"]:
        assert item["evidence_ids"] and set(item["evidence_ids"])<=ids


def test_query_facts_copy_actual_values_and_cite_exact_cells():
    rows=[{"initial_bank":"ATLAS","success_rate_pct":85.33,"net_contribution_paise":392582},
          {"initial_bank":"DELTA","success_rate_pct":84.56,"net_contribution_paise":-206330}]
    report=query_report(rows)
    original=deepcopy(report)
    answer=present_query(report)["answer"]
    assert_separated(answer)
    assert "85.33%" in answer["facts"][0]["text"]
    assert "INR 3,925.82" in answer["facts"][0]["text"]
    assert "INR -2,063.30" in answer["facts"][1]["text"]
    assert answer["facts"][1]["evidence"][0]["row_index"]==1
    assert answer["explanations"][0]["status"]=="not_established"
    assert "negative-contribution" in answer["recommendations"][0]["text"]
    assert report==original


@pytest.mark.parametrize("intent",["overview","trend"])
def test_every_exploratory_answer_has_all_three_sections(intent):
    report=query_report([{"business_date":"2026-11-15","net_contribution_paise":1200}],intent=intent)
    assert_separated(present_query(report)["answer"])


def test_empty_results_and_nulls_do_not_invent_zero_or_causes():
    empty=present_query(query_report([],columns=["net_contribution_paise"]))["answer"]
    assert_separated(empty)
    assert "no matching" in empty["facts"][0]["text"]
    null=present_query(query_report([{"net_contribution_paise":None}]))["answer"]
    assert "not available" in null["facts"][0]["text"]
    assert "INR 0" not in null["facts"][0]["text"]


def test_trend_describes_observed_endpoints_without_unsupported_causal_change():
    rows=[{"business_date":f"2026-11-{i:02}","net_contribution_paise":i*100} for i in range(15,22)]
    answer=present_query(query_report(rows,intent="trend"))["answer"]
    assert [f["evidence"][0]["row_index"] for f in answer["facts"]]==[0,6]
    assert "same weekdays" in answer["recommendations"][0]["text"]


def test_ranking_without_supported_decline_still_explains_hypothesis_limit():
    report={"segments":[],"query_evidence":[]}
    answer=present(report,"Show priorities",{"plan":{"intent":"priorities"}})["answer"]
    assert_separated(answer)
    assert answer["explanations"][0]["status"]=="not_established"
    assert "No supported priority" in answer["title"]


def test_accounting_explanation_requires_observed_reconciled_components():
    row={"initial_bank":"DELTA","merchant_fee_paise":10000,"processing_cost_paise":12000,
         "loss_provision_paise":500,"net_contribution_paise":-2500}
    answer=present_query(query_report([row]))["answer"]
    assert answer["explanations"][0]["status"]=="verified_data"
    assert all(value in answer["explanations"][0]["text"] for value in ("INR 100.00","INR 120.00","INR -25.00"))
    assert "net_contribution_paise" in answer["explanations"][0]["evidence"][0]["columns"]
    row["net_contribution_paise"]=-2501
    assert present_query(query_report([row]))["answer"]["explanations"][0]["status"]=="not_established"


def test_a_conversion_gap_is_not_presented_as_a_verified_provider_cause():
    segment={"segment_id":"ATLAS__UPI","bank":"ATLAS","payment_method":"UPI","rank":None,
        "current_completed_orders":100,"current_successful_orders":80,"pending_orders":0,
        "current_net_contribution_paise":5000,"current_success_rate":0.8,"benchmark_success_rate":0.95,
        "gap_percentage_points":15,"gap_interval_percentage_points":[5,20]}
    answer=present({"segments":[segment]},"Why did conversion drop?",{"plan":{"intent":"drivers"}})["answer"]
    assert answer["hypotheses"]==[]
    assert answer["explanations"][0]["status"]=="verified_data"
    assert "underlying cause is not verified" in answer["explanations"][0]["text"]
    assert "provider or routing" not in str(answer).lower()
