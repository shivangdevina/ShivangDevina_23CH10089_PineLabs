from copy import deepcopy
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from payments_analyst.api import app,get_repository
from payments_analyst.contracts import AnalysisBlocked,BackendUnavailable
from payments_analyst.presentation import present_query,apply_research
from payments_analyst.research import ReadOnlyResearchAgent,ReadOnlyResearchStore,research_transaction
from payments_analyst.sql_analyst import SQLPlan


def report():
    value={"dataset":{"id":"507681f6-c5b1-5c50-b7dc-4216e29745e3"},
        "filters":{"banks":[],"payment_methods":[]},"segments":[],"query_evidence":[],
        "planning":{"plan":{"intent":"overview"}},"sql_result":{"generated_sql":"SELECT initial_bank, sum(net_contribution_paise) AS net_contribution_paise FROM data WHERE period='current' GROUP BY initial_bank","columns":["initial_bank","net_contribution_paise"],
        "rows":[{"initial_bank":"DELTA","net_contribution_paise":-2500}]}}
    return present_query(value)


def test_research_exposes_only_aggregate_read_and_one_bounded_investigation():
    assert not hasattr(ReadOnlyResearchStore(),"save_analysis")
    assert not hasattr(ReadOnlyResearchStore(),"save_scenario")
    calls=[]
    class Reader:
        def execute(self,request,sql):
            calls.append((request,sql))
            assert request.banks==["DELTA"]
            return {"rows":[],"evidence":{"execution":"NOT_EXECUTED"}}
    def planner(question,dataset):
        assert "merchant fees" in question and "DELTA" in question
        return SQLPlan(decision="analyse",intent="overview",sql="SELECT sum(net_contribution_paise) AS net_contribution_paise FROM data",clarification="")
    value=report();original=deepcopy(value)
    outcome=ReadOnlyResearchAgent(Reader(),planner).run(value)
    assert len(calls)==1 and outcome["queries_executed"]==1 and value==original
    assert outcome["permissions"]=="SELECT only"


def test_research_failure_preserves_primary_answer_and_makes_no_claims():
    class Reader:
        def execute(self,*args):
            raise BackendUnavailable("Cannot read")
    plan=lambda *args:SQLPlan(decision="analyse",intent="overview",sql="SELECT count(*) FROM data",clarification="")
    value=report();before=deepcopy(value["answer"])
    outcome=ReadOnlyResearchAgent(Reader(),plan).run(value)
    assert outcome["status"]=="unavailable" and outcome["queries_executed"]==0
    assert apply_research(value,outcome)["answer"]==before


def test_supporting_cost_check_is_numerical_and_cites_research_rows():
    value=report()
    research={"status":"completed","purpose":"cost_breakdown","target_bank":"DELTA","expected_target_net_paise":-2500,"query_evidence":[],
      "result":{"rows":[{"initial_bank":"DELTA","initial_payment_method":"CARD","merchant_fee_paise":10000,
                            "processing_cost_paise":12000,"loss_provision_paise":500,"net_contribution_paise":-2500}]}}
    result=apply_research(value,research)
    explanation=result["answer"]["explanations"][0]
    assert explanation["status"]=="verified_data"
    assert "INR 120.00" in explanation["text"] and "INR -25.00" in explanation["text"]
    assert explanation["evidence"][0]["query_id"]=="research-agent-sql"


def test_research_failure_codes_must_reconcile_before_they_become_a_finding():
    value=report();value["segments"]=[{"bank":"ATLAS","payment_method":"UPI","current_completed_orders":100,"current_successful_orders":80}]
    research={"status":"completed","purpose":"failure_codes","target_bank":"ATLAS","target_method":"UPI","query_evidence":[],
       "result":{"rows":[{"last_failure_code":"NETWORK_ERROR","failed_orders":19}]}}
    before=deepcopy(value["answer"])
    result=apply_research(value,research)
    assert result["answer"]==before and "did not reconcile" in research["verification"]


def test_research_transaction_switches_role_and_verifies_read_only(monkeypatch):
    from contextlib import contextmanager
    from payments_analyst import research
    calls=[]
    class Connection:
        def execute(self,sql):
            calls.append(sql);return self
        def fetchone(self):
            return {"role":"payment_analyst_research","read_only":"on"}
    @contextmanager
    def transaction(user_id,read_only):
        assert user_id is None and read_only is True
        yield Connection()
    monkeypatch.setattr(research,"runtime_transaction",transaction)
    with research_transaction():
        pass
    assert calls[0]=="SET LOCAL ROLE payment_analyst_research"


def test_stream_reports_real_steps_and_does_not_invent_result_on_failure(monkeypatch):
    from payments_analyst import api
    monkeypatch.setattr(api,"generate_plan",lambda *args:SQLPlan(decision="analyse",intent="overview",sql="SELECT count(*) FROM data",clarification=""))
    class Repository:
        def list_datasets(self,user):
            return [{"id":"507681f6-c5b1-5c50-b7dc-4216e29745e3"}]
        def load_snapshot(self,*args):
            raise AnalysisBlocked("Totals do not reconcile")
    app.dependency_overrides[get_repository]=Repository
    try:
        with TestClient(app) as client:
            response=client.post("/chat/stream",json={"dataset_id":"507681f6-c5b1-5c50-b7dc-4216e29745e3","question":"Show success rate"})
            assert response.status_code==200
            assert '"type": "step"' in response.text and '"type": "error"' in response.text
            assert '"type": "result"' not in response.text
    finally:
        app.dependency_overrides.pop(get_repository,None)


def test_research_does_not_broaden_an_exploratory_category_filter():
    value=report()
    value["sql_result"]["generated_sql"]="SELECT initial_bank, sum(net_contribution_paise) AS net_contribution_paise FROM data WHERE period='current' AND merchant_category='DIGITAL' GROUP BY initial_bank"
    outcome=ReadOnlyResearchAgent(planner=lambda *args:pytest.fail("Do not broaden category scope")).run(value)
    assert outcome["status"]=="not_needed" and outcome["queries_executed"]==0


@pytest.mark.parametrize("where",["period <> 'current'","period='current' OR initial_payment_method='CARD'","period='current' AND initial_payment_method <> 'CARD'"])
def test_research_withholds_nonpositive_or_ambiguous_sql_scopes(where):
    from payments_analyst.research import _current_bank_method_scope
    assert _current_bank_method_scope("SELECT count(*) FROM data WHERE "+where) is None


def test_research_preserves_method_equality_not_present_in_ui_filters():
    value=report()
    value["sql_result"]["generated_sql"]="SELECT initial_bank,sum(net_contribution_paise) AS net_contribution_paise FROM data WHERE period='current' AND initial_payment_method='CARD' GROUP BY initial_bank"
    class Reader:
        def execute(self,request,query):
            assert request.payment_methods==["CARD"]
            return {"rows":[],"evidence":{}}
    planner=lambda *args:SQLPlan(decision="analyse",intent="overview",sql="SELECT count(*) FROM data",clarification="")
    outcome=ReadOnlyResearchAgent(Reader(),planner).run(value)
    assert outcome["scope"]["payment_methods"]==["CARD"]


def test_cost_identity_does_not_validate_a_different_period_or_cohort():
    value=report();before=deepcopy(value["answer"])
    research={"status":"completed","purpose":"cost_breakdown","target_bank":"DELTA","expected_target_net_paise":-2500,"query_evidence":[],
       "result":{"rows":[{"initial_bank":"DELTA","initial_payment_method":"CARD","merchant_fee_paise":10000,
                            "processing_cost_paise":13000,"loss_provision_paise":500,"net_contribution_paise":-3500}]}}
    assert apply_research(value,research)["answer"]==before
    assert "did not reconcile" in research["verification"]
