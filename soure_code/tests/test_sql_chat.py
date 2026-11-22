from uuid import UUID
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from payments_analyst.api import app, get_repository
from payments_analyst.contracts import AnalysisBlocked, BackendUnavailable
from payments_analyst.sql_analyst import generate_plan, validate_sql
from test_api import TestRepository
from payments_analyst.preview_routes import fixture_snapshot


@pytest.mark.parametrize("query", [
    "DROP TABLE payments.orders",
    "SELECT count(*) FROM data; DELETE FROM data",
    "WITH x AS (DELETE FROM payments.orders RETURNING *) SELECT count(*) FROM x",
    "SELECT count(*) INTO another FROM data",
    "SELECT count(*) FROM payments.analysis_runs",
    "SELECT count(*) FROM auth.users",
    "SELECT count(*) FROM data, data d",
    "SELECT count(*) FROM data JOIN data d ON true",
    "SELECT count(*) FROM data UNION SELECT count(*) FROM data",
    "SELECT (SELECT count(*) FROM auth.users), count(*) FROM data",
    "SELECT pg_sleep(3), count(*) FROM data",
    "SELECT set_config('app.public_workspace','false',true), count(*) FROM data",
    "SELECT public.sum(amount_paise) FROM data",
    "SELECT sum(amount_paise)::regclass FROM data",
    "SELECT *, count(*) FROM data GROUP BY initial_bank",
    "SELECT count(*) OVER () FROM data",
    "SELECT merchant_id, count(*) FROM data GROUP BY merchant_id",
    "SELECT 99999 AS net_revenue_paise, count(*) FROM data",
    "SELECT sum(99999) AS revenue_paise FROM data",
    "SELECT count(*) AS completed_orders_count FROM data",
    "SELECT 100.0*sum(successful_orders)/count(*) AS success_rate_pct FROM data",
    "SELECT sum(amount_paise) AS net_contribution_paise FROM data",
    "SELECT count(*) AS completed_orders FROM data",
])
def test_unsafe_or_misleading_generated_sql_is_blocked(query):
    with pytest.raises(AnalysisBlocked):
        validate_sql(query)


@pytest.mark.parametrize("query", [
    "SELECT initial_bank, sum(completed_orders) AS completed_orders FROM data WHERE period='current' GROUP BY initial_bank",
    "SELECT business_date, sum(net_contribution_paise) AS net_contribution_paise FROM data GROUP BY business_date ORDER BY business_date",
    "SELECT initial_payment_method, round(100.0*sum(successful_orders)/nullif(sum(completed_orders),0),2) AS success_rate_pct FROM data GROUP BY initial_payment_method",
    "SELECT last_failure_code, count(*) AS failures FROM data WHERE final_status='FAILED' GROUP BY last_failure_code",
])
def test_varied_valid_aggregate_queries_pass(query):
    assert validate_sql(query).startswith("SELECT")


def test_gemini_generates_sql_and_receives_followup_context(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-private")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    def handle(request):
        body=json.loads(request.content)
        context=json.loads(body["messages"][1]["content"])
        assert context["previous_question"] == "Show UPI performance"
        assert context["question"] == "Break that down by bank"
        assert "sql" in body["response_format"]["json_schema"]["schema"]["properties"]
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps({
            "decision":"analyse", "intent":"overview", "sql":"SELECT initial_bank, count(*) AS orders FROM data GROUP BY initial_bank", "clarification":""})}}]})
    plan=generate_plan("Break that down by bank",{}, {"question":"Show UPI performance"},transport=httpx.MockTransport(handle))
    assert "GROUP BY initial_bank" in plan.sql


def test_model_outage_never_substitutes_numbers(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-private")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    with pytest.raises(BackendUnavailable):
        generate_plan("Show bank performance",{},transport=httpx.MockTransport(lambda request:httpx.Response(503)))


def test_no_login_public_api_and_private_history_stays_separate():
    repository=TestRepository(fixture_snapshot("incidents"))
    app.dependency_overrides[get_repository]=lambda:repository
    try:
        with TestClient(app) as client:
            assert client.get("/datasets").status_code == 200
            assert client.get("/app-config").json()["access_mode"] == "public_shared"
            saved=client.post("/analyses",json={"dataset_id":repository.snapshot.version["id"]}).json()
            assert repository.analyses[saved["id"]][0] is None
            assert client.get(f"/analyses/{saved['id']}").status_code == 200
            repository.analyses[saved["id"]]=(UUID("f1a6276e-74c5-4a6e-976e-24ac0c2e2e97"),saved)
            assert client.get(f"/analyses/{saved['id']}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_chat_preserves_followup_filters_and_saves_query_evidence(monkeypatch):
    from payments_analyst import api
    from payments_analyst.sql_analyst import SQLPlan
    from payments_analyst.contracts import AnalysisRequest
    repository=TestRepository(fixture_snapshot("incidents"))
    old={"question":"Show UPI performance", "dataset":{"id":repository.snapshot.version["id"]},
         "filters":{"banks":[],"payment_methods":["UPI"]},"sql_result":{"generated_sql":"prior SQL"}}
    previous=repository.save_analysis(None,AnalysisRequest(dataset_id=repository.snapshot.version["id"]),old)
    def plan(question,dataset,prior):
        assert prior["question"]=="Show UPI performance"
        return SQLPlan(decision="analyse",intent="overview",sql="SELECT initial_bank, count(*) AS orders FROM data GROUP BY initial_bank",clarification="")
    monkeypatch.setattr(api,"generate_plan",plan)
    def execute(user,request,query):
        assert user is None and request.payment_methods==["UPI"]
        return {"generated_sql":query,"columns":["initial_bank","orders"],"rows":[],
                "evidence":{"execution":"NOT_EXECUTED","template_id":"test-double"}}
    repository.execute_generated=execute
    app.dependency_overrides[get_repository]=lambda:repository
    try:
        with TestClient(app) as client:
            response=client.post("/chat",json={"dataset_id":repository.snapshot.version["id"],"question":"Break that down by bank","previous_analysis_id":previous["id"]})
            assert response.status_code==200
            report=response.json()["analysis"]
            assert report["filters"]["payment_methods"]==["UPI"]
            assert report["planning"]["mode"]=="gemini_sql"
            assert all(report["answer"][section] for section in ("facts","explanations","recommendations"))
            assert report["answer"]["explanations"][0]["status"]=="not_established"
            assert report["query_evidence"][-1]["template_id"]=="test-double"
            assert client.post("/chat",json={"dataset_id":repository.snapshot.version["id"],"question":"Show banks","previous_analysis_id":"invalid"}).status_code==422
    finally:
        app.dependency_overrides.clear()


def test_chat_quality_failure_blocks_before_generated_query(monkeypatch):
    from copy import deepcopy
    from payments_analyst import api
    from payments_analyst.sql_analyst import SQLPlan
    repository=TestRepository(deepcopy(fixture_snapshot("incidents")))
    repository.snapshot.diagnostics["order_count"]+=1
    monkeypatch.setattr(api,"generate_plan",lambda *args:SQLPlan(decision="analyse",intent="overview",sql="SELECT count(*) FROM data",clarification=""))
    def forbidden(*args):
        pytest.fail("Generated SQL must not execute on unreconciled data")
    repository.execute_generated=forbidden
    app.dependency_overrides[get_repository]=lambda:repository
    try:
        with TestClient(app) as client:
            response=client.post("/chat",json={"dataset_id":repository.snapshot.version["id"],"question":"Show bank performance"})
            assert response.status_code==409 and not repository.analyses
    finally:
        app.dependency_overrides.clear()
