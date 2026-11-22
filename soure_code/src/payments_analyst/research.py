"""Bounded research agent: one optional AI-generated investigation, SELECT-only tools."""

from contextlib import contextmanager
from copy import deepcopy
from pglast import parse_sql

from .access import research_user
from .contracts import AnalysisBlocked, AnalysisRequest, BackendUnavailable
from .repository import execute_scoped_query, runtime_transaction
from .sql_analyst import generate_plan


@contextmanager
def research_transaction():
    with runtime_transaction(research_user.get(), read_only=True) as connection:
        connection.execute("SET LOCAL ROLE payment_analyst_research")
        identity = connection.execute("SELECT current_user AS role, current_setting('transaction_read_only') AS read_only").fetchone()
        if identity["role"] != "payment_analyst_research" or identity["read_only"] != "on":
            raise BackendUnavailable("Research requires its dedicated read-only database role.")
        yield connection


class ReadOnlyResearchStore:
    """Only an aggregate-read capability; no save, update, merge or payment tool."""
    __slots__ = ()

    def execute(self, request, query):
        result = execute_scoped_query(research_transaction, request, query, current_only=True)
        result["evidence"]["template_id"] = "research-agent-sql"
        result["evidence"]["database_role"] = "payment_analyst_research"
        return result


def _current_bank_method_scope(query):
    """Do not silently investigate a broader cohort than a custom exploratory query."""
    try:
        tree = parse_sql(query)[0].stmt()
    except Exception:
        return None
    constraints = {}
    def conjunction(node):
        if not isinstance(node, dict):
            return False
        if node.get("@") == "BoolExpr":
            return node["boolop"]["name"] == "AND_EXPR" and all(conjunction(arg) for arg in node["args"])
        if node.get("@") != "A_Expr" or node["kind"]["name"] != "AEXPR_OP" or [part["sval"] for part in node["name"]] != ["="]:
            return False
        left, right = node.get("lexpr") or {}, node.get("rexpr") or {}
        if right.get("@") == "ColumnRef":
            left, right = right, left
        if left.get("@") != "ColumnRef" or right.get("@") != "A_Const" or (right.get("val") or {}).get("@") != "String":
            return False
        column, value = left["fields"][-1].get("sval"), right["val"]["sval"]
        if column not in {"period", "initial_bank", "initial_payment_method"} or (column in constraints and constraints[column] != value):
            return False
        constraints[column] = value
        return True
    return constraints if conjunction(tree.get("whereClause")) and constraints.get("period") == "current" else None


class ReadOnlyResearchAgent:
    __slots__ = ("_reader", "_planner")

    def __init__(self, reader=None, planner=None):
        self._reader = reader or ReadOnlyResearchStore()
        self._planner = planner or generate_plan

    def run(self, report):
        # The agent cannot mutate the report that the orchestrator will persist.
        report = deepcopy(report)
        result = {"agent": "read_only_research", "permissions": "SELECT only", "max_queries": 1,
                  "status": "not_needed", "queries_executed": 0, "query_evidence": [],
                  "summary": "Reviewed the available results; no additional check was needed."}
        ranked = sorted((s for s in report.get("segments", []) if s.get("rank")), key=lambda s: s["rank"])
        negative = next((row for row in report["sql_result"]["rows"]
                         if isinstance(row.get("net_contribution_paise"), (int, float))
                         and row["net_contribution_paise"] < 0 and row.get("initial_bank")), None)
        if ranked:
            target = ranked[0]
            bank, method = target["bank"], target["payment_method"]
            purpose = "failure_codes"
            question = f"For {bank} {method} in the current snapshot, count completed failed orders by last_failure_code. Return last_failure_code and count(*) AS failed_orders."
            summary = f"Checked the recorded failure codes for {bank} {method}."
        elif negative and negative["initial_bank"] in {"ATLAS", "BREEZE", "CEDAR", "DELTA"}:
            scope = _current_bank_method_scope(report["sql_result"].get("generated_sql", ""))
            if (scope is None or
                any(negative.get(key) is not None for key in ("merchant_category", "business_date", "amount_band"))):
                return {**result, "summary": "Kept the question's custom scope; no broader automatic cost check was run."}
            bank, method = negative["initial_bank"], negative.get("initial_payment_method") or scope.get("initial_payment_method")
            if scope.get("initial_bank", bank) != bank or (negative.get("initial_payment_method") and scope.get("initial_payment_method", method) != method):
                return result
            if method not in {None, "CARD", "UPI"}:
                return result
            purpose = "cost_breakdown"
            question = f"For {bank} {method or ''} in the current snapshot, show merchant fees, processing costs, modelled loss provision and net contribution totals by initial_bank and initial_payment_method. Use the source column names as aliases."
            summary = f"Checked fees and costs for {bank} in the current snapshot."
        else:
            return result
        # Research scopes are explicit and recorded; they never broaden a selected filter.
        selected = report["filters"]
        if selected.get("banks") and bank not in selected["banks"]:
            return result
        methods = [method] if method else selected.get("payment_methods", [])
        if selected.get("payment_methods") and method and method not in selected["payment_methods"]:
            return result
        request = AnalysisRequest(dataset_id=report["dataset"]["id"], banks=[bank], payment_methods=methods)
        result.update(purpose=purpose, scope={**request.model_dump(mode="json"), "period": "current"},
                      target_bank=bank, target_method=method, investigation=question)
        if purpose == "cost_breakdown":
            result["expected_target_net_paise"] = negative["net_contribution_paise"]
        try:
            plan = self._planner(question, report["dataset"])
            if plan.decision != "analyse":
                return {**result, "status": "withheld", "summary": "The additional check could not be planned reliably."}
            query = self._reader.execute(request, plan.sql)
            return {**result, "status": "completed", "queries_executed": 1, "summary": summary,
                    "result": query, "query_evidence": [query["evidence"]]}
        except (AnalysisBlocked, BackendUnavailable):
            # Primary results remain usable; never invent supporting evidence.
            return {**result, "status": "unavailable", "summary": "The additional check was unavailable; the answer uses only the completed primary analysis."}
