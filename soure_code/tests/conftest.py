import pytest


@pytest.fixture(autouse=True)
def isolate_api_research():
    """API unit tests never call live Gemini/Supabase through the secondary agent."""
    from payments_analyst.api import app,get_research_agent
    class NoExternalResearch:
        def run(self,report):
            return {"agent":"read_only_research","status":"not_needed","permissions":"SELECT only",
                    "summary":"Unit-test research double; no provider or database calls.","queries_executed":0,"query_evidence":[]}
    app.dependency_overrides[get_research_agent]=NoExternalResearch
    yield
    app.dependency_overrides.pop(get_research_agent,None)
