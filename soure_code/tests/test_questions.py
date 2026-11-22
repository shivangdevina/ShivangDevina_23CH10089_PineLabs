from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from payments_analyst.api import app
from payments_analyst.contracts import BackendUnavailable, QuestionRequest
from payments_analyst.planner import cloud_plan, guided_plan, plan_question
from payments_analyst.preview_routes import fixture_snapshot


def question(text, **kwargs):
    return QuestionRequest(dataset_id=uuid4(), question=text, **kwargs)


def test_guided_plan_extracts_only_explicit_supported_filters():
    request, info = plan_question(question("Why did ATLAS UPI payment success drop?"), force_guided=True)
    assert request.banks == ["ATLAS"]
    assert request.payment_methods == ["UPI"]
    assert info["plan"]["intent"] == "drivers"
    assert info["mode"] == "guided"


@pytest.mark.parametrize("text", [
    "Why did success drop yesterday?", "Show payment performance in 2025",
    "Show the last 14 days of payments", "Show merchant performance",
    "Show attempt success rate", "Delete the payments table", "Show HDFC payments",
    "What if we recover 50%?", "Show payments except ATLAS", "How is the weather?",
])
def test_unsupported_questions_need_clarification(text):
    assert guided_plan(text).decision == "clarify"


def test_filter_conflict_requires_clarification_instead_of_broadening():
    request, result = plan_question(question("Show UPI payments", payment_methods=["CARD"]), force_guided=True)
    assert request is None
    assert result["status"] == "NEEDS_CLARIFICATION"


def test_explicit_question_narrows_retained_ui_filters():
    request, _ = plan_question(question("Show UPI payments", banks=["ATLAS"]), force_guided=True)
    assert request.banks == ["ATLAS"]
    assert request.payment_methods == ["UPI"]


@pytest.fixture
def gemini_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "private-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "configured-test-model")


def plan_payload(**overrides):
    return {"decision": "analyse", "intent": "priorities", "banks": [], "payment_methods": [],
            "time_scope": "current_snapshot", "clarification": "", **overrides}


def test_gemini_uses_google_endpoint_and_structured_schema(gemini_config):
    import json
    def handle(request):
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        body = json.loads(request.content)
        assert body["model"] == "configured-test-model"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "sql" not in body["response_format"]["json_schema"]["schema"]["properties"]
        assert "private-test-key" not in request.url.query.decode()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(plan_payload())}}]})
    assert cloud_plan("Which payment issue should we fix first?", transport=httpx.MockTransport(handle)).intent == "priorities"


def test_schema_valid_hallucinated_filter_is_rejected(gemini_config):
    import json
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(plan_payload(banks=["ATLAS"]))}}]}))
    with pytest.raises(BackendUnavailable, match="filter"):
        cloud_plan("Which payment issue should we fix first?", transport=transport)


@pytest.mark.parametrize("content", ['{"sql": "DROP TABLE orders"}', 'not valid JSON'])
def test_malformed_model_output_is_not_executed(gemini_config, content):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(BackendUnavailable, match="invalid plan"):
        cloud_plan("Show payment performance", transport=transport)


def test_provider_outage_has_no_fabricated_fallback(gemini_config):
    with pytest.raises(BackendUnavailable):
        cloud_plan("Show payment performance", transport=httpx.MockTransport(lambda request: httpx.Response(503)))


def test_static_dashboard_and_explicit_preview_lifecycle(monkeypatch):
    monkeypatch.setenv("ENABLE_SYNTHETIC_PREVIEW", "true")
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "PayScope" in page.text
        assert client.get("/assets/app.js").status_code == 200
        datasets = client.get("/preview/datasets").json()["datasets"]
        incident = next(d for d in datasets if d["fixture"] == "incidents")
        response = client.post("/preview/questions", json={"dataset_id": incident["id"],
            "fixture": "incidents", "question": "Which payment issue should we fix first?"})
        assert response.status_code == 200
        saved = response.json()
        assert saved["saved"] is False and saved["id"] is None
        report = saved["analysis"]
        assert report["planning"]["mode"] == "guided"
        assert report["overview"]["completed_orders"] == 2237
        assert all(q["execution"] == "NOT_EXECUTED" for q in report["query_evidence"])
        assert report["answer"]["facts"][0]["kind"] == "observed"
        scenario = client.post("/preview/scenarios", json={"fixture": "incidents",
            "analysis_request": report["filters"], "scenario": {"segment_id": "ATLAS__UPI", "recovery_fraction": 1}})
        assert scenario.status_code == 200
        atlas = next(s for s in report["segments"] if s["segment_id"] == "ATLAS__UPI")
        assert scenario.json()["scenario"]["result"] == atlas["reference_scenario"]


def test_preview_disabled_is_not_an_implicit_database_fallback(monkeypatch):
    monkeypatch.setenv("ENABLE_SYNTHETIC_PREVIEW", "false")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "")
    with TestClient(app) as client:
        assert client.get("/preview/datasets").status_code == 404
        assert client.get("/datasets").status_code == 503


def test_duplicate_data_demo_blocks_instead_of_showing_stale_findings(monkeypatch):
    monkeypatch.setenv("ENABLE_SYNTHETIC_PREVIEW", "true")
    identifier = fixture_snapshot("incidents").version["id"]
    with TestClient(app) as client:
        response = client.post("/preview/questions", json={"dataset_id": identifier,
            "fixture": "data_quality_failure", "question": "Show payment performance"})
        assert response.status_code == 409
        assert response.json()["status"] == "BLOCKED"
        recovered = client.post("/preview/questions", json={"dataset_id": identifier,
            "fixture": "incidents", "question": "Show payment performance"})
        assert recovered.status_code == 200  # exception fixture never corrupts cached source data


def test_public_configuration_never_exposes_private_credentials(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "private-owner-connection")
    monkeypatch.setenv("SUPABASE_RUNTIME_DB_URL", "private-runtime-connection")
    monkeypatch.setenv("GEMINI_API_KEY", "private-gemini-key")
    with TestClient(app) as client:
        text = client.get("/app-config").text
        assert "private-owner-connection" not in text
        assert "private-runtime-connection" not in text
        assert "private-gemini-key" not in text


def test_clarification_happens_without_running_analysis(monkeypatch):
    monkeypatch.setenv("ENABLE_SYNTHETIC_PREVIEW", "true")
    identifier = fixture_snapshot("incidents").version["id"]
    with TestClient(app) as client:
        response = client.post("/preview/questions", json={"dataset_id": identifier,
            "question": "Show payments yesterday"})
        assert response.json()["status"] == "NEEDS_CLARIFICATION"
        assert "analysis" not in response.json()


def test_authenticated_question_flows_through_gemini_plan_to_evidence(monkeypatch):
    from uuid import UUID
    from payments_analyst.api import get_repository
    from payments_analyst.api import get_access_subject as require_user
    from payments_analyst import planner
    from test_api import TestRepository
    user = UUID("f1a6276e-74c5-4a6e-976e-24ac0c2e2e97")
    repository = TestRepository(fixture_snapshot("incidents"))
    monkeypatch.setenv("PLANNER_MODE", "gemini")
    calls = []
    def gemini(question, *, provider):
        calls.append((question, provider))
        return planner.LanguagePlan(**plan_payload(intent="drivers", banks=["ATLAS"], payment_methods=["UPI"]))
    monkeypatch.setattr(planner, "cloud_plan", gemini)
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[get_repository] = lambda: repository
    try:
        with TestClient(app) as client:
            result = client.post("/questions", json={"dataset_id": repository.snapshot.version["id"],
                "question": "Why did ATLAS UPI success rates drop?"})
            assert result.status_code == 200
            report = result.json()["analysis"]
            assert calls[0][1] == "gemini"
            assert report["planning"]["mode"] == "gemini"
            assert report["filters"]["banks"] == ["ATLAS"]
            assert report["filters"]["payment_methods"] == ["UPI"]
            assert len(report["segments"]) == 1
            assert report["answer"]["facts"][0]["kind"] == "observed"
            assert len(repository.analyses) == 1
    finally:
        app.dependency_overrides.clear()


def test_live_gemini_failure_does_not_return_preview_results(monkeypatch):
    from payments_analyst.api import get_repository
    from payments_analyst.api import get_access_subject as require_user
    from payments_analyst import planner
    class NoQueriesExpected:
        def load_snapshot(self, *args):
            pytest.fail("No database query should execute after an invalid language plan.")
    monkeypatch.setenv("PLANNER_MODE", "gemini")
    def failed(*args, **kwargs):
        raise BackendUnavailable("Gemini is unavailable.")
    monkeypatch.setattr(planner, "cloud_plan", failed)
    app.dependency_overrides[require_user] = lambda: uuid4()
    app.dependency_overrides[get_repository] = NoQueriesExpected
    try:
        with TestClient(app) as client:
            result = client.post("/questions", json={"dataset_id": str(uuid4()), "question": "Show payment performance"})
            assert result.status_code == 503
            assert "analysis" not in result.json()
    finally:
        app.dependency_overrides.clear()
