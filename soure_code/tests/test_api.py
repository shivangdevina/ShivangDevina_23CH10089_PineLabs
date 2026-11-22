from copy import deepcopy
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest

from payments_analyst.api import app, get_repository
from payments_analyst.api import get_access_subject as require_user
from payments_analyst.auth import verify_access_token
from payments_analyst.contracts import BackendUnavailable, ResourceNotFound
from payments_analyst.data import generate
from payments_analyst.preview import snapshot_from_generated

USER = UUID("f1a6276e-74c5-4a6e-976e-24ac0c2e2e97")
OTHER_USER = UUID("92d4d231-2627-4e25-8773-5ea30d58f421")


@pytest.fixture(scope="module")
def snapshot():
    return snapshot_from_generated(generate())


class TestRepository:
    """Test double only. The real API has no in-memory repository mode."""
    __test__ = False

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.analyses = {}
        self.scenarios = {}

    def list_datasets(self, user_id):
        return [{"id": self.snapshot.version["id"], "is_synthetic": True}]

    def load_snapshot(self, user_id, request):
        if str(request.dataset_id) != self.snapshot.version["id"]:
            raise ResourceNotFound("Dataset not found.")
        return self.snapshot

    def save_analysis(self, user_id, request, report):
        identifier = str(uuid4())
        result = {"id": identifier, "status": "COMPLETED", "analysis": report}
        self.analyses[identifier] = (user_id, result)
        return result

    def get_analysis(self, user_id, identifier):
        row = self.analyses.get(str(identifier))
        if not row or row[0] != user_id:
            raise ResourceNotFound("Analysis not found.")
        return row[1]

    def save_scenario(self, user_id, analysis_id, request, result):
        identifier = str(uuid4())
        response = {"id": identifier, "analysis_id": str(analysis_id), "scenario": result}
        self.scenarios[identifier] = (user_id, response)
        return response

    def get_scenario(self, user_id, identifier):
        row = self.scenarios.get(str(identifier))
        if not row or row[0] != user_id:
            raise ResourceNotFound("Scenario not found.")
        return row[1]


@pytest.fixture
def client(snapshot):
    repository = TestRepository(snapshot)
    app.dependency_overrides[require_user] = lambda: USER
    app.dependency_overrides[get_repository] = lambda: repository
    with TestClient(app) as http:
        yield http, repository
    app.dependency_overrides.clear()


def test_api_analysis_evidence_and_scenario_round_trip(client, snapshot):
    http, repository = client
    response = http.post("/analyses", json={"dataset_id": snapshot.version["id"]})
    assert response.status_code == 201
    saved = response.json()
    identifier = saved["id"]
    assert http.get(f"/analyses/{identifier}").json() == saved
    evidence = http.get(f"/analyses/{identifier}/evidence").json()
    assert evidence["queries"][0]["execution"] == "NOT_EXECUTED"  # honest test-double provenance
    scenario = http.post(f"/analyses/{identifier}/scenarios", json={
        "segment_id": "ATLAS__UPI", "recovery_fraction": 1})
    assert scenario.status_code == 201
    result = scenario.json()
    atlas = next(s for s in saved["analysis"]["segments"] if s["segment_id"] == "ATLAS__UPI")
    assert result["scenario"]["result"] == atlas["reference_scenario"]
    assert http.get(f"/scenarios/{result['id']}").json() == result


def test_data_quality_failure_returns_blocked_without_saving_analysis(client, snapshot):
    http, repository = client
    repository.snapshot = deepcopy(snapshot)
    repository.snapshot.diagnostics["order_count"] += 1
    response = http.post("/analyses", json={"dataset_id": snapshot.version["id"]})
    assert response.status_code == 409
    assert response.json()["status"] == "BLOCKED"
    assert repository.analyses == {}


def test_other_user_cannot_retrieve_analysis_or_start_scenario(client, snapshot):
    http, repository = client
    saved = http.post("/analyses", json={"dataset_id": snapshot.version["id"]}).json()
    app.dependency_overrides[require_user] = lambda: OTHER_USER
    assert http.get(f"/analyses/{saved['id']}").status_code == 404
    assert http.post(f"/analyses/{saved['id']}/scenarios", json={"segment_id": "ATLAS__UPI"}).status_code == 404


@pytest.mark.parametrize("extra", [
    {"sql": "DROP TABLE payments.orders"},
    {"banks": ["ATLAS'; DROP TABLE payments.orders; --"]},
    {"intervention_cost_paise": -1},
    {"intervention_cost_paise": 0.1},
    {"owner_id": str(OTHER_USER)},
])
def test_unsafe_or_unsupported_inputs_are_rejected(client, snapshot, extra):
    http, _ = client
    response = http.post("/analyses", json={"dataset_id": snapshot.version["id"], **extra})
    assert response.status_code == 422


def test_missing_dataset_returns_not_found(client):
    http, _ = client
    assert http.post("/analyses", json={"dataset_id": str(uuid4())}).status_code == 404


def test_public_api_reports_missing_database_without_login_prompt(monkeypatch):
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "")
    with TestClient(app) as http:
        assert http.get("/health").json()["database_readiness"] == "not_checked"
        assert http.get("/datasets").status_code == 503
        assert http.post("/analyses", json={"dataset_id": str(uuid4())}).status_code == 503


@pytest.fixture
def auth_environment(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://testproject.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")


def test_access_token_is_verified_by_supabase(auth_environment):
    def handler(request):
        assert str(request.url) == "https://testproject.supabase.co/auth/v1/user"
        assert request.headers["Authorization"] == "Bearer test-user-token"
        assert request.headers["apikey"] == "sb_publishable_test"
        return httpx.Response(200, json={"id": str(USER), "role": "authenticated"})
    assert verify_access_token("test-user-token", transport=httpx.MockTransport(handler)) == USER


def test_expired_authentication_rejected(auth_environment):
    with pytest.raises(HTTPException) as error:
        verify_access_token("expired", transport=httpx.MockTransport(lambda request: httpx.Response(401)))
    assert error.value.status_code == 401


def test_authentication_outage_fails_closed_without_leaking_token(auth_environment):
    with pytest.raises(BackendUnavailable) as error:
        verify_access_token("secret-token", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert "secret-token" not in str(error.value)


def test_secret_key_is_not_accepted_as_publishable_key(auth_environment, monkeypatch):
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_secret_do_not_use")
    with pytest.raises(BackendUnavailable):
        verify_access_token("token")


def test_no_runtime_database_does_not_fall_back_to_owner_or_memory(monkeypatch):
    from payments_analyst import repository
    monkeypatch.setattr(repository, "load_environment", lambda: None)
    monkeypatch.delenv("SUPABASE_RUNTIME_DB_URL", raising=False)
    with pytest.raises(BackendUnavailable, match="restricted"):
        repository.SupabaseRepository().list_datasets(USER)


def test_runtime_database_rejects_owner_identity(monkeypatch):
    from payments_analyst import repository

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query, parameters=None):
            return self

        def fetchone(self):
            return {"name": "postgres", "rolsuper": False, "rolbypassrls": True}

    monkeypatch.setattr(repository, "load_environment", lambda: None)
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "postgresql://postgres:private@db.project.supabase.co/postgres")
    monkeypatch.setattr(repository.psycopg, "connect", lambda *args, **kwargs: Connection())
    with pytest.raises(BackendUnavailable, match="administrative credentials are refused"):
        repository.SupabaseRepository().list_datasets(USER)
