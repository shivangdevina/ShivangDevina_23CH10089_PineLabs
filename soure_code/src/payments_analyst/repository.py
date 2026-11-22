"""Live repository. All persistent reads and writes go to Supabase PostgreSQL."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from uuid import UUID, uuid4
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from fastapi.encoders import jsonable_encoder

from .contracts import (AnalysisBlocked, AnalysisRequest, BackendUnavailable,
                        CALCULATION_VERSION, ResourceNotFound, Snapshot)
from .database import load_environment, validate_connection_url
from .access import access_mode, data_customer
from .cache import get_cache
from .queries import DAILY_SQL, DIAGNOSTICS_SQL, LIST_DATASETS_SQL, VERSION_SQL


@contextmanager
def runtime_transaction(user_id: UUID | None, *, read_only=True):
    load_environment()
    value = os.environ.get("SUPABASE_RUNTIME_DB_URL", "")
    if not value:
        raise BackendUnavailable("Configure the restricted Supabase runtime connection after migrations.")
    try:
        validate_connection_url(value)
        with psycopg.connect(value, sslmode="require", connect_timeout=10, prepare_threshold=None,
                             application_name="payment-analyst-api", row_factory=dict_row) as connection:
            connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            connection.read_only = read_only
            identity = connection.execute("""SELECT current_user AS name, rolsuper, rolbypassrls
                FROM pg_roles WHERE rolname = current_user""").fetchone()
            if identity["name"] != "payment_analyst_runtime" or identity["rolsuper"] or identity["rolbypassrls"]:
                raise BackendUnavailable("The API requires payment_analyst_runtime; administrative credentials are refused.")
            connection.execute("SET LOCAL statement_timeout = '5s'")
            connection.execute("SET LOCAL lock_timeout = '2s'")
            # Public scope is chosen by the server, never supplied by a request body.
            connection.execute("SELECT set_config('app.public_workspace', %s, true)",
                               ("true" if user_id is None and access_mode() == "public_demo" else "false",))
            connection.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user_id) if user_id else "",))
            connection.execute("SELECT set_config('request.jwt.claims', %s, true)",
                               (json.dumps({"sub": str(user_id), "role": "authenticated"} if user_id else {"role": "anon"}),))
            connection.execute("SELECT set_config('app.data_customer_id', %s, true)", (str(data_customer.get() or ""),))
            yield connection
    except (psycopg.Error, ValueError) as error:
        if isinstance(error, (AnalysisBlocked, ResourceNotFound)):
            raise
        raise BackendUnavailable("Supabase query failed. Check migrations, runtime permissions and connection settings.") from None


def execute_scoped_query(transaction, request, query, *, current_only=False):
    from .sql_analyst import SCOPED_DATA_SQL, validate_sql
    query = validate_sql(query)
    parameters = {"dataset_id": str(request.dataset_id), "banks": request.banks or None,
                  "methods": request.payment_methods or None}
    # Escaping percent signs preserves SQL modulo/LIKE syntax in parameterized execution.
    source = SCOPED_DATA_SQL
    if current_only:
        if "f.created_at >= d.history_start" not in source:
            raise BackendUnavailable("Research window scoping is unavailable.")
        source = source.replace("f.created_at >= d.history_start", "f.created_at >= d.current_start")
    statement = source + "SELECT * FROM (" + query.replace("%", "%%") + ") AS answer LIMIT 501"
    with transaction() as connection:
        connection.execute("SET LOCAL search_path = pg_catalog")
        cursor = connection.execute(statement, parameters)
        columns = [column.name for column in cursor.description]
        rows = cursor.fetchall()
    if len(rows) > 500 or len(columns) != len(set(columns)):
        raise AnalysisBlocked("Query returned too many rows or duplicate column names. Narrow the question.")
    evidence = {"template_id": "gemini-generated-sql", "sql": statement, "parameters": parameters,
                "row_count": len(rows), "execution": "EXECUTED", "executed_at": datetime.now(timezone.utc).isoformat(),
                "source_window": "current" if current_only else "current_and_history"}
    return {"generated_sql": query, "columns": columns, "rows": jsonable_encoder(rows),
            "evidence": evidence, "row_limit": 500}

class SupabaseRepository:
    def __init__(self, cache=None):
        self.cache = cache if cache is not None else get_cache()

    def _cache_context(self, connection, user_id, request, version):
        # Refresh scope from Supabase BEFORE Redis, including reassignment/revocation.
        scope = connection.execute("""SELECT app_private.access_profile() AS profile,
            coalesce((SELECT jsonb_agg(v.merchant_id ORDER BY v.merchant_id)
            FROM app_private.visible_merchants() v WHERE v.dataset_id=%s),'[]'::jsonb) AS merchants""",
            (request.dataset_id,)).fetchone()
        project = urlparse(os.environ.get("SUPABASE_RUNTIME_DB_URL", ""))
        return {"project": {"host": project.hostname, "login": project.username, "database": project.path},
                "mode": access_mode(), "user_id": str(user_id) if user_id else None,
                "profile": scope["profile"], "customer_scope": str(data_customer.get() or ""),
                "merchants": scope["merchants"], "dataset": version,
                "banks": sorted(set(request.banks)), "methods": sorted(set(request.payment_methods)),
                "calculation_version": CALCULATION_VERSION}

    def access_profile(self, user_id):
        with runtime_transaction(user_id) as connection:
            return connection.execute("SELECT app_private.access_profile() AS profile").fetchone()["profile"]

    def save_action_plan(self, analysis_id, plan, user_id=None):
        identifier = uuid4()
        with runtime_transaction(user_id, read_only=False) as connection:
            connection.execute("""INSERT INTO payments.action_plans(id,analysis_id,dataset_id,calculation_version,result,owner_id,is_public,customer_id)
                SELECT %s,a.id,a.dataset_id,%s,%s,%s,%s,a.customer_id FROM payments.analysis_runs a WHERE a.id=%s""",
                (identifier,plan["calculation_version"],Jsonb(plan),user_id,user_id is None,analysis_id))
        return {"id":str(identifier),"analysis_id":str(analysis_id),"plan":plan}

    def get_action_plan(self, identifier, user_id=None):
        with runtime_transaction(user_id) as connection:
            row = connection.execute("SELECT id,analysis_id,result FROM payments.action_plans WHERE id=%s", (identifier,)).fetchone()
        if not row:
            raise ResourceNotFound("Action plan not found.")
        return {"id":str(row["id"]),"analysis_id":str(row["analysis_id"]),"plan":row["result"]}

    def save_action_simulation(self, plan_id, request, result, user_id=None):
        identifier = uuid4()
        with runtime_transaction(user_id, read_only=False) as connection:
            connection.execute("""INSERT INTO payments.action_simulations(id,action_plan_id,inputs,result,calculation_version)
                VALUES (%s,%s,%s,%s,%s)""", (identifier,plan_id,Jsonb(request.model_dump(mode="json")),Jsonb(result),result["calculation_version"]))
        return {"id":str(identifier),"action_plan_id":str(plan_id),"scenario":result}

    def get_action_simulation(self, identifier, user_id=None):
        with runtime_transaction(user_id) as connection:
            row = connection.execute("SELECT id,action_plan_id,result FROM payments.action_simulations WHERE id=%s", (identifier,)).fetchone()
        if not row:
            raise ResourceNotFound("Action simulation not found.")
        return {"id":str(row["id"]),"action_plan_id":str(row["action_plan_id"]),"scenario":row["result"]}

    def save_query_evaluation(self, result, user_id=None):
        identifier = uuid4()
        with runtime_transaction(user_id, read_only=False) as connection:
            connection.execute("""INSERT INTO payments.query_evaluation_runs
                (id, dataset_id, model, prompt_hash, suite_version, validator_version, dataset_fingerprint, status, result)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (identifier, result["dataset_id"], result["model"], result["prompt_hash"], result["suite_version"],
                 result["validator_version"], result["dataset_fingerprint"], result["status"], Jsonb(result)))
        return {"id": str(identifier), "evaluation": result}

    def latest_query_evaluation(self, dataset_id, user_id=None):
        with runtime_transaction(user_id) as connection:
            row = connection.execute("""SELECT e.id, e.result, d.fingerprint AS current_fingerprint
                FROM payments.query_evaluation_runs e JOIN payments.dataset_versions d ON d.id = e.dataset_id
                WHERE e.dataset_id = %s ORDER BY e.created_at DESC, e.id DESC LIMIT 1""", (dataset_id,)).fetchone()
        if not row:
            return {"status": "NOT_RUN", "evaluation": None}
        from .query_evaluation import matches_current_configuration
        result = row["result"]
        current = matches_current_configuration(result, row["current_fingerprint"])
        return {"id": str(row["id"]), "status": result["status"] if current else "STALE",
                "matches_current_configuration": current, "evaluation": result}

    def execute_generated(self, user_id, request, query):
        if not self.cache.active:
            return execute_scoped_query(lambda: runtime_transaction(user_id), request, query)
        from .sql_analyst import validate_sql, SCOPED_DATA_SQL, SQL_VALIDATOR_VERSION
        query = validate_sql(query)
        with runtime_transaction(user_id) as connection:
            versions = connection.execute(VERSION_SQL, {"dataset_id": str(request.dataset_id)}).fetchall()
            if not versions:
                raise ResourceNotFound("Dataset not found.")
            material = self._cache_context(connection, user_id, request, dict(versions[0]))
            material.update(kind="generated-sql", query=query, source=SCOPED_DATA_SQL, validator=SQL_VALIDATOR_VERSION)
            key = self.cache.key(material)
            read = self.cache.read(key)
            value = read.value
            if (read.status == "HIT" and isinstance(value, dict) and value.get("generated_sql") == query
                    and isinstance(value.get("columns"), list) and all(isinstance(column, str) for column in value["columns"])
                    and len(value["columns"]) == len(set(value["columns"]))
                    and isinstance(value.get("rows"), list) and len(value["rows"]) <= 500
                    and all(isinstance(row, dict) and set(row) == set(value["columns"]) for row in value["rows"])
                    and isinstance(value.get("evidence"), dict) and value["evidence"].get("execution") == "EXECUTED"):
                value["evidence"] = {**value["evidence"], "execution": "CACHE_HIT", "cache_provider": "upstash",
                    "cache_retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "cache_note": "Reused a previously executed query after a fresh Supabase access/scope check."}
                return value
            @contextmanager
            def transaction():
                yield connection
            result = execute_scoped_query(transaction, request, query)
        self.cache.write(key, result)
        result["evidence"]["cache_status"] = read.status if read.status != "HIT" else "INVALID"
        return result

    def list_datasets(self, user_id):
        with runtime_transaction(user_id) as connection:
            return connection.execute(LIST_DATASETS_SQL).fetchall()

    def load_snapshot(self, user_id, request: AnalysisRequest) -> Snapshot:
        parameters = {"dataset_id": str(request.dataset_id), "banks": sorted(set(request.banks)) or None,
                      "methods": sorted(set(request.payment_methods)) or None}
        evidence = []
        with runtime_transaction(user_id) as connection:
            def execute(template_id, query, values):
                rows = connection.execute(query, values).fetchall()
                evidence.append({"template_id": template_id, "sql": query, "parameters": values,
                                 "row_count": len(rows), "execution": "EXECUTED",
                                 "executed_at": datetime.now(timezone.utc).isoformat()})
                return rows

            versions = execute("dataset-version-v1", VERSION_SQL, {"dataset_id": parameters["dataset_id"]})
            if not versions:
                raise ResourceNotFound("Dataset not found.")
            diagnostics = execute("source-reconciliation-v1", DIAGNOSTICS_SQL,
                                  {"dataset_id": parameters["dataset_id"]})[0]
            daily, key = None, None
            if self.cache.active:
                material = self._cache_context(connection, user_id, request, dict(versions[0]))
                material.update(kind="daily-aggregates", query=DAILY_SQL, diagnostics=diagnostics)
                key = self.cache.key(material)
                read = self.cache.read(key)
                cached = read.value
                if (read.status == "HIT" and isinstance(cached, dict) and isinstance(cached.get("daily"), list)
                        and len(cached["daily"]) <= 20_000 and isinstance(cached.get("evidence"), dict)
                        and cached["evidence"].get("execution") == "EXECUTED"):
                    try:
                        from .analytics import _validate_snapshot
                        _validate_snapshot(Snapshot(dict(versions[0]), cached["daily"], dict(diagnostics), []))
                        from datetime import date
                        if not all(isinstance(row["business_date"], date) for row in cached["daily"]):
                            raise ValueError("Invalid cached dates.")
                        daily = cached["daily"]
                        evidence.append({**cached["evidence"], "execution": "CACHE_HIT", "cache_provider": "upstash",
                            "cache_retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "cache_note": "Reused aggregates; source reconciliation and access were checked in Supabase again."})
                    except (AnalysisBlocked, ValueError, TypeError, KeyError, AttributeError):
                        daily = None
            if daily is None:
                daily = execute("daily-matched-cohorts-v1", DAILY_SQL, parameters)
                if key is not None and self.cache.active and len(daily) <= 20_000:
                    self.cache.write(key, {"daily": daily, "evidence": evidence[-1]})
            if len(daily) > 20_000:
                raise AnalysisBlocked("Aggregate result exceeds this demo's analysis limit.")
        return Snapshot(dict(versions[0]), daily, dict(diagnostics), evidence)

    def save_analysis(self, user_id, request, report):
        identifier = uuid4()
        with runtime_transaction(user_id, read_only=False) as connection:
            row = connection.execute("""INSERT INTO payments.analysis_runs
                (id, owner_id, is_public, customer_id, dataset_id, question, status, analysis_plan, query_evidence,
                 result, warnings, calculation_version)
                VALUES (%s, %s, %s, app_private.data_customer(), %s, %s, 'COMPLETED', %s, %s, %s, %s, %s) RETURNING customer_id""",
                (identifier, user_id, user_id is None, request.dataset_id, report.get("question", "Structured impact analysis"),
                 Jsonb(request.model_dump(mode="json")), Jsonb(report["query_evidence"]),
                 Jsonb(report), Jsonb(report["caveats"]), CALCULATION_VERSION)).fetchone()
        return {"id": str(identifier), "status": "COMPLETED", "analysis": report,
                "customer_scope": str(row["customer_id"]) if row["customer_id"] else None}

    def get_analysis(self, user_id, identifier):
        with runtime_transaction(user_id) as connection:
            row = connection.execute("""SELECT id, status, result, customer_id FROM payments.analysis_runs
                WHERE id = %s""", (identifier,)).fetchone()
        if not row:
            raise ResourceNotFound("Analysis not found.")
        return {"id": str(row["id"]), "status": row["status"], "analysis": row["result"],
                "customer_scope": str(row["customer_id"]) if row["customer_id"] else None}

    def save_scenario(self, user_id, analysis_id, request, result):
        identifier = uuid4()
        with runtime_transaction(user_id, read_only=False) as connection:
            connection.execute("""INSERT INTO payments.scenarios
                (id, analysis_id, owner_id, is_public, customer_id, inputs, result, calculation_version)
                SELECT %s,a.id,%s,%s,a.customer_id,%s,%s,%s FROM payments.analysis_runs a WHERE a.id=%s""",
                (identifier, user_id, user_id is None, Jsonb(request.model_dump(mode="json")),
                 Jsonb(result), CALCULATION_VERSION, analysis_id))
        return {"id": str(identifier), "analysis_id": str(analysis_id), "scenario": result}

    def get_scenario(self, user_id, identifier):
        with runtime_transaction(user_id) as connection:
            row = connection.execute("""SELECT id, analysis_id, result FROM payments.scenarios
                WHERE id = %s""", (identifier,)).fetchone()
        if not row:
            raise ResourceNotFound("Scenario not found.")
        return {"id": str(row["id"]), "analysis_id": str(row["analysis_id"]), "scenario": row["result"]}
