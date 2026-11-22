from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from payments_analyst.api import app, get_repository
from payments_analyst.contracts import AnalysisBlocked, BackendUnavailable
from payments_analyst.data import DatasetConfig, generate
from payments_analyst.query_evaluation import (CASES, EvaluationCase, compare_results,
    expected_rows, independent_facts, run_evaluation)
from payments_analyst.sql_analyst import SQLPlan


@pytest.fixture(scope="module")
def data():
    return generate(DatasetConfig(orders_per_day=10))


def test_oracle_uses_raw_orders_and_ledger_without_sql_view(data):
    facts=independent_facts(data)
    case=CASES[0]
    expected=expected_rows(facts,case)[0]
    orders=[o for o in data.tables["orders"] if o["created_at"]>=data.version["current_start"]]
    ids={o["id"] for o in orders}
    assert expected["completed_orders"]==sum(o["final_status"]!="PENDING" for o in orders)
    assert expected["pending_orders"]==sum(o["final_status"]=="PENDING" for o in orders)
    assert expected["net_contribution_paise"]==sum(e["amount_paise"] for e in data.tables["financial_events"] if e["order_id"] in ids)


def test_comparator_ignores_row_order_and_accepts_equivalent_aliases():
    case=CASES[1]
    expected=[{"initial_bank":"ATLAS","completed_orders":100,"success_rate_pct":92.1234,"net_contribution_paise":12345},
              {"initial_bank":"DELTA","completed_orders":200,"success_rate_pct":90.0,"net_contribution_paise":-500}]
    actual=[{"bank":"DELTA","completed_orders_count":200,"success_rate_pct":90,"net_contribution_paise":-500},
            {"bank":"ATLAS","completed_orders_count":100,"success_rate_pct":92.12,"net_contribution_paise":12345}]
    assert compare_results(actual,expected,case)[0]
    actual[1]["net_contribution_paise"]+=1
    assert not compare_results(actual,expected,case)[0]


@pytest.mark.parametrize("actual",[
    [{"completed_orders":70}],
    [{"completed_orders":69}],
    [{"completed_orders":None}],
    [{"completed_orders":float("nan")}],
    [{"completed_orders":70},{"completed_orders":70}],
    [{"another_metric":70}],
])
def test_comparator_rejects_missing_wrong_duplicate_null_or_nonfinite_answers(actual):
    case=EvaluationCase("one","Count orders",metrics=("completed_orders",))
    assert not compare_results(actual,[{"completed_orders":71}],case)[0]


def test_comparator_does_not_accept_wrong_groups():
    case=EvaluationCase("bank","Count by bank",("initial_bank",),("completed_orders",))
    assert not compare_results([{"initial_bank":"ATLAS","completed_orders":71}],
        [{"initial_bank":"DELTA","completed_orders":71}],case)[0]


class BenchmarkRepository:
    def __init__(self,data):
        self.data=data
        self.executions=[]
    def list_datasets(self,user):
        return [{**self.data.version,"fingerprint":self.data.fingerprint()}]
    def execute_generated(self,user,request,query):
        self.executions.append(query)
        return {"rows":[{"completed_orders":0}],"evidence":{"execution":"NOT_EXECUTED","note":"Test double"}}


def test_evaluator_catches_valid_sql_with_wrong_business_answer(data):
    repository=BenchmarkRepository(data)
    case=EvaluationCase("wrong","Count current completed orders",metrics=("completed_orders",))
    def planner(*args):
        return SQLPlan(decision="analyse",intent="overview",sql="SELECT sum(completed_orders) AS completed_orders FROM data WHERE period='history'",clarification="")
    result=run_evaluation(UUID(data.version["id"]),repository,planner=planner,cases=(case,))
    assert result["status"]=="FAIL" and result["failed"]==1
    assert "Wrong completed_orders" in result["cases"][0]["detail"]
    assert result["cases"][0]["sql_executed"]


def test_unsafe_case_is_rejected_without_executing_model_sql(data):
    repository=BenchmarkRepository(data)
    def planner(*args):
        return SQLPlan(decision="analyse",intent="overview",sql="DROP TABLE payments.orders",clarification="")
    result=run_evaluation(UUID(data.version["id"]),repository,planner=planner,cases=(CASES[9],))
    assert result["status"]=="PASS" and not repository.executions
    assert not result["cases"][0]["sql_executed"]


def test_provider_failure_is_error_not_pass_or_fabricated_fallback(data):
    repository=BenchmarkRepository(data)
    def planner(*args):
        raise BackendUnavailable("Provider unavailable")
    result=run_evaluation(UUID(data.version["id"]),repository,planner=planner,cases=(CASES[0],))
    assert result["status"]=="FAIL" and result["errors"]==1 and result["passed"]==0


def test_followup_without_passing_parent_is_skipped_and_counts_against_score(data):
    repository=BenchmarkRepository(data)
    result=run_evaluation(UUID(data.version["id"]),repository,planner=lambda *args:pytest.fail("No parent context"),cases=(CASES[7],))
    assert result["skipped"]==1 and result["passed"]==0 and result["status"]=="FAIL"


def test_fixture_fingerprint_mismatch_blocks_before_calling_model(data):
    repository=BenchmarkRepository(data)
    repository.list_datasets=lambda user:[{**data.version,"fingerprint":"0"*64}]
    with pytest.raises(AnalysisBlocked,match="fingerprint"):
        run_evaluation(UUID(data.version["id"]),repository,planner=lambda *args:pytest.fail("No model call"))


def test_latest_evaluation_is_read_only_and_visitors_cannot_launch_suite():
    class Repository:
        def latest_query_evaluation(self,dataset_id,user_id=None):
            return {"status":"NOT_RUN","evaluation":None}
    app.dependency_overrides[get_repository]=Repository
    try:
        with TestClient(app) as client:
            assert client.get("/query-evaluations/latest",params={"dataset_id":"507681f6-c5b1-5c50-b7dc-4216e29745e3"}).json()["status"]=="NOT_RUN"
            assert client.post("/query-evaluations/latest").status_code==405
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("question",["Show success last month","Show payments yesterday","Show this week success"])
def test_ambiguous_time_preflight_does_not_call_gemini(question):
    import httpx
    from payments_analyst.sql_analyst import generate_plan
    transport=httpx.MockTransport(lambda request:pytest.fail("Do not guess a relative calendar period"))
    plan=generate_plan(question,{},transport=transport)
    assert plan.decision=="clarify" and plan.planning_source=="preflight" and plan.sql==""
    assert "planning_source" not in SQLPlan.model_json_schema()["properties"]


@pytest.mark.parametrize("field",["model","prompt_hash","suite_version","validator_version","dataset_fingerprint"])
def test_model_prompt_schema_or_dataset_change_invalidates_old_score(monkeypatch,field):
    from payments_analyst.query_evaluation import matches_current_configuration,prompt_hash,SUITE_VERSION
    from payments_analyst.sql_analyst import SQL_VALIDATOR_VERSION
    monkeypatch.setenv("GEMINI_MODEL","current-model")
    report={"model":"current-model","prompt_hash":prompt_hash(),"suite_version":SUITE_VERSION,
            "validator_version":SQL_VALIDATOR_VERSION,"dataset_fingerprint":"a"*64}
    assert matches_current_configuration(report,"a"*64)
    report[field]="different"
    assert not matches_current_configuration(report,"a"*64)
