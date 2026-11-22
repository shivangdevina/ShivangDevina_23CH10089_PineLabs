from collections import defaultdict
from copy import deepcopy
from decimal import Decimal

import pytest

from payments_analyst.analytics import analyse,scenario_from_analysis
from payments_analyst.contracts import AnalysisRequest,ScenarioRequest,AnalysisBlocked
from payments_analyst.data import generate,basis_points,RETRYABLE_FAILURES
from payments_analyst.preview import snapshot_from_generated
from payments_analyst.next_actions import (NextActionRequest,ActionSimulationRequest,rank_actions,
    action_scenario,action_request,NextActionsWorkflow,ACTION_RESEARCH_SQL)


@pytest.fixture(scope="module")
def fixture():
    data=generate();request=AnalysisRequest(dataset_id=data.version["id"])
    report=analyse(snapshot_from_generated(data),request)
    merchants={m["id"]:m for m in data.tables["merchants"]}
    last={}
    for attempt in data.tables["payment_attempts"]:
        if attempt["order_id"] not in last or attempt["attempt_number"]>last[attempt["order_id"]]["attempt_number"]:
            last[attempt["order_id"]]=attempt
    rules={(r["bank"],r["payment_method"]):r for r in data.tables["cost_rules"]}
    groups={}
    for order in data.tables["orders"]:
        if not (data.version["current_start"]<=order["created_at"]<data.version["window_end"]):continue
        attempt=last[order["id"]]
        if order["final_status"]!="FAILED" or order["finalized_at"]>data.version["as_of"] or attempt["failure_code"] not in RETRYABLE_FAILURES:continue
        key=(order["initial_bank"],order["initial_payment_method"],attempt["failure_code"])
        row=groups.setdefault(key,dict(bank=key[0],payment_method=key[1],failure_code=key[2],eligible_orders=0,unpriced_orders=0,
          eligible_payment_value_paise=0,retry_fee_paise=0,retry_success_cost_paise=0,retry_failure_cost_paise=0,retry_loss_provision_paise=0))
        rule=rules[key[:2]];merchant=merchants[order["merchant_id"]];amount=order["amount_paise"]
        row["eligible_orders"]+=1;row["eligible_payment_value_paise"]+=amount
        row["retry_fee_paise"]+=basis_points(amount,merchant["fee_bps"])+merchant["fee_fixed_paise"]
        row["retry_success_cost_paise"]+=basis_points(amount,rule["success_bps"])+rule["success_fixed_paise"]
        row["retry_failure_cost_paise"]+=rule["failure_fixed_paise"]
        row["retry_loss_provision_paise"]+=basis_points(amount,rule["expected_loss_bps"])
    return report,list(groups.values())


def test_combined_action_matches_existing_financial_engine(fixture):
    report,rows=fixture
    actions=rank_actions(report,rows,NextActionRequest())
    both=next(a for a in actions if a["action_id"]=="ATLAS__UPI__BOTH")
    original=scenario_from_analysis(report,ScenarioRequest(segment_id="ATLAS__UPI",recovery_fraction=Decimal('.5')))
    assert both["scenario"]["result"]==original["result"]
    assert both["expected_net_paise"]==12674
    assert both["eligible_orders"]==53


def test_ranks_use_positive_lower_net_bounds_and_are_reproducible(fixture):
    report,rows=fixture
    actions=rank_actions(report,rows,NextActionRequest())
    ranked=[a for a in actions if a["rank"]]
    assert ranked and all(a["conservative_net_paise"]>0 for a in ranked)
    assert [a["conservative_net_paise"] for a in ranked]==sorted([a["conservative_net_paise"] for a in ranked],reverse=True)
    assert actions==rank_actions(report,list(reversed(rows)),NextActionRequest())
    assert all(not a["rank"] for a in actions if a["segment_id"]=="DELTA__CARD")


def test_additional_cost_changes_recommendation_and_simulation_exactly(fixture):
    report,rows=fixture
    zero=rank_actions(report,rows,NextActionRequest())
    costly=rank_actions(report,rows,NextActionRequest(intervention_cost_paise=20000))
    a=next(x for x in zero if x["action_id"]=="ATLAS__UPI__BOTH")
    b=next(x for x in costly if x["action_id"]==a["action_id"])
    assert b["expected_net_paise"]==a["expected_net_paise"]-20000==-7326
    assert b["rank"] is None
    assert action_scenario(b,ActionSimulationRequest(action_id=b["action_id"],recovery_fraction=1,intervention_cost_paise=20000))["result"]["incremental_net_contribution_paise"]==5983


def test_overlapping_actions_partition_economics_without_double_counting(fixture):
    report,rows=fixture
    actions=rank_actions(report,rows,NextActionRequest())
    subset={a["action_id"].split("__")[-1]:a for a in actions if a["segment_id"]=="ATLAS__UPI"}
    for field,value in subset["BOTH"]["economics"].items():
        assert subset["NETWORK"]["economics"][field]+subset["TIMEOUT"]["economics"][field]==value
    assert abs(sum(Decimal(subset[k]["full_gap_successes"]) for k in ("NETWORK","TIMEOUT"))-Decimal(subset["BOTH"]["full_gap_successes"]))<Decimal('1e-20')


@pytest.mark.parametrize("change",["duplicate","missing","cost","code"])
def test_bad_research_blocks_action_ranking(fixture,change):
    report,rows=fixture;rows=deepcopy(rows)
    if change=="duplicate":rows.append(rows[0])
    elif change=="missing":rows.pop()
    elif change=="cost":rows[0]["retry_fee_paise"]+=1
    else:rows[0]["failure_code"]="UNSUPPORTED"
    with pytest.raises(AnalysisBlocked):rank_actions(report,rows,NextActionRequest())


def test_action_simulation_does_not_mutate_saved_economics(fixture):
    report,rows=fixture
    candidate=next(a for a in rank_actions(report,rows,NextActionRequest()) if a["action_id"]=="ATLAS__UPI__BOTH")
    before=deepcopy(candidate)
    result=action_scenario(candidate,ActionSimulationRequest(action_id=candidate["action_id"],recovery_fraction=0))
    assert result["result"]["additional_successful_orders"]==0 and result["result"]["incremental_net_contribution_paise"]<0
    assert candidate==before and len(result["curve"])==21


def test_custom_category_or_history_scope_is_not_broadened():
    base={"segments":[],"filters":{"dataset_id":"507681f6-c5b1-5c50-b7dc-4216e29745e3"},"sql_result":{}}
    for sql in ["SELECT count(*) FROM data WHERE period='history'","SELECT count(*) FROM data WHERE period='current' AND merchant_category='DIGITAL'"]:
        base["sql_result"]["generated_sql"]=sql
        with pytest.raises(AnalysisBlocked):action_request(base)


def test_workflow_has_only_a_read_capability_and_explicit_assumptions(fixture):
    report,rows=fixture
    class Reader:
        def read(self,request):
            return rows,{"execution":"NOT_EXECUTED","note":"Independent test fixture"}
    result=NextActionsWorkflow(Reader()).run(report,NextActionRequest())
    assert result["research"]["permissions"]=="SELECT only"
    assert result["ranked_count"]>0 and "overlap" in " ".join(result["assumptions"])
    assert result["baseline"]["extra_net_paise"]==0


def test_action_pricing_query_is_one_select_and_current_bounded():
    from pglast import parse_sql
    import re
    parsed=parse_sql(re.sub(r"%\([a-z_]+\)s", "NULL", ACTION_RESEARCH_SQL))
    assert len(parsed)==1 and type(parsed[0].stmt).__name__=="SelectStmt"
    assert "f.created_at>=d.current_start" in ACTION_RESEARCH_SQL


def test_public_action_plan_and_simulation_api_roundtrip(fixture):
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from payments_analyst.api import app,get_repository,get_next_actions_workflow,get_access_subject
    from payments_analyst.contracts import ResourceNotFound
    from test_api import TestRepository
    report,rows=fixture
    class Repository(TestRepository):
        def __init__(self):
            super().__init__(snapshot_from_generated(generate()));self.plans={};self.action_sims={};self.plan_owners={};self.sim_owners={}
        def save_action_plan(self,analysis_id,plan,user_id=None):
            identifier=str(uuid4());value={"id":identifier,"analysis_id":str(analysis_id),"plan":plan};self.plans[identifier]=value;self.plan_owners[identifier]=user_id;return value
        def get_action_plan(self,identifier,user_id=None):
            if str(identifier) not in self.plans or self.plan_owners[str(identifier)]!=user_id:raise ResourceNotFound("Action plan not found")
            return self.plans[str(identifier)]
        def save_action_simulation(self,plan_id,request,result,user_id=None):
            identifier=str(uuid4());value={"id":identifier,"action_plan_id":str(plan_id),"scenario":result};self.action_sims[identifier]=value;self.sim_owners[identifier]=user_id;return value
        def get_action_simulation(self,identifier,user_id=None):
            if str(identifier) not in self.action_sims or self.sim_owners[str(identifier)]!=user_id:raise ResourceNotFound("Simulation not found")
            return self.action_sims[str(identifier)]
    class Reader:
        def read(self,request):return rows,{"execution":"NOT_EXECUTED","note":"Unit fixture"}
    repository=Repository()
    app.dependency_overrides[get_repository]=lambda:repository
    app.dependency_overrides[get_next_actions_workflow]=lambda:NextActionsWorkflow(Reader())
    try:
        with TestClient(app) as client:
            saved=client.post("/analyses",json={"dataset_id":report["dataset"]["id"]}).json()
            response=client.post(f"/analyses/{saved['id']}/next-actions",json={"recovery_fraction":0.5})
            assert response.status_code==201,response.text
            plan=response.json();assert client.get(f"/action-plans/{plan['id']}").json()==plan
            assert plan["plan"]["ranked_count"]>0
            sim=client.post(f"/action-plans/{plan['id']}/simulations",json={"action_id":"ATLAS__UPI__BOTH","recovery_fraction":0.5,"intervention_cost_paise":20000})
            assert sim.status_code==201
            simulation=sim.json();assert simulation["scenario"]["result"]["incremental_net_contribution_paise"]==-7326
            assert client.get(f"/action-simulations/{simulation['id']}").json()==simulation
            assert client.post(f"/analyses/{saved['id']}/next-actions",json={"intervention_cost_paise":-1}).status_code==422
            app.dependency_overrides[get_access_subject]=lambda:uuid4()
            assert client.get(f"/action-plans/{plan['id']}").status_code==404
            assert client.get(f"/action-simulations/{simulation['id']}").status_code==404
            assert client.post(f"/action-plans/{plan['id']}/simulations",json={"action_id":"ATLAS__UPI__BOTH"}).status_code==404
            app.dependency_overrides.pop(get_access_subject,None)
            repository.analyses[saved['id']]=(uuid4(),saved)
            assert client.post(f"/analyses/{saved['id']}/next-actions",json={}).status_code==404
    finally:
        app.dependency_overrides.pop(get_access_subject,None)
        app.dependency_overrides.pop(get_repository,None)
        app.dependency_overrides.pop(get_next_actions_workflow,None)


def test_current_daily_grouping_keeps_the_same_cohort_for_action_research():
    request=action_request({"segments":[],"filters":{"dataset_id":"507681f6-c5b1-5c50-b7dc-4216e29745e3","payment_methods":["UPI"]},
      "sql_result":{"generated_sql":"SELECT business_date,sum(net_contribution_paise) AS net_contribution_paise FROM data WHERE period='current' AND initial_payment_method='UPI' GROUP BY business_date","columns":["business_date","net_contribution_paise"]}})
    assert request.payment_methods==["UPI"] and request.banks==[]
