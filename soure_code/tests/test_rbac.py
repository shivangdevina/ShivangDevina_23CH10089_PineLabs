"""Access-boundary tests. Live fixtures are rolled back, with no accounts provisioned."""
from contextlib import contextmanager
import json
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from payments_analyst import api
from payments_analyst.access import access_mode, research_identity, research_user, customer_scope, data_customer
from payments_analyst.contracts import BackendUnavailable, AnalysisRequest


@pytest.mark.parametrize('path,body', [('/datasets',None),('/me',None),('/analyses',{}),('/chat',{}),('/chat/stream',{}),
 ('/query-evaluations/latest?dataset_id='+str(uuid4()),None),('/analyses/'+str(uuid4())+'/next-actions',{}),
 ('/action-plans/'+str(uuid4()),None),('/action-plans/'+str(uuid4())+'/simulations',{}),
 ('/action-simulations/'+str(uuid4()),None),('/scenarios/'+str(uuid4()),None)])
def test_private_mode_requires_authentication_everywhere(monkeypatch,path,body):
    monkeypatch.setenv('DATA_ACCESS_MODE','rbac')
    with TestClient(api.app) as client:
        response=client.get(path) if body is None else client.post(path,json=body)
        assert response.status_code==401


def test_unassigned_verified_account_fails_closed(monkeypatch):
    monkeypatch.setenv('DATA_ACCESS_MODE','rbac')
    monkeypatch.setattr(api,'require_user',lambda credentials:uuid4())
    monkeypatch.setattr(api.SupabaseRepository,'access_profile',lambda self,user:None)
    with TestClient(api.app) as client:
        assert client.get('/datasets',headers={'Authorization':'Bearer ignored-test-token'}).status_code==403


def test_invalid_mode_does_not_enable_public_access(monkeypatch):
    monkeypatch.setenv('DATA_ACCESS_MODE','anything')
    with pytest.raises(BackendUnavailable):access_mode()


def test_demo_profile_keeps_open_access_without_role_selection(monkeypatch):
    monkeypatch.setenv('DATA_ACCESS_MODE','public_demo')
    with TestClient(api.app) as client:
        assert client.get('/me').json()['role']=='demo'
        config=client.get('/app-config').json()
        assert config['access_mode']=='public_shared'
        assert set(config['roles'])=={'pine_admin','customer'}
        assert client.post('/me',json={'role':'pine_admin'}).status_code==405


def test_customer_cannot_read_global_benchmark(monkeypatch):
    class Repository:
        def access_profile(self,user_id):return {'role':'customer'}
        def latest_query_evaluation(self,*args,**kwargs):raise AssertionError('Must not read global results')
    api.app.dependency_overrides[api.get_access_subject]=lambda:uuid4()
    api.app.dependency_overrides[api.get_repository]=Repository
    try:
        with TestClient(api.app) as client:
            assert client.get('/query-evaluations/latest?dataset_id='+str(uuid4())).status_code==403
    finally:api.app.dependency_overrides.clear()


def test_workflow_identity_and_scope_are_reset_after_failure():
    user,customer=uuid4(),uuid4()
    with pytest.raises(RuntimeError):
        with research_identity(user),customer_scope(customer):
            assert research_user.get()==user and data_customer.get()==customer
            raise RuntimeError('Test interruption')
    assert research_user.get() is None and data_customer.get() is None


def test_live_membership_predicates_with_rollback_only_fixtures():
    if os.environ.get('RUN_SUPABASE_RBAC_TESTS')!='1':
        pytest.skip('Opt-in, rollback-only Supabase checks.')
    from psycopg.rows import dict_row
    from payments_analyst.database import connect
    connection=connect();connection.row_factory=dict_row
    try:
        dataset=connection.execute('SELECT id FROM payments.dataset_versions ORDER BY imported_at DESC LIMIT 1').fetchone()['id']
        merchants=connection.execute('SELECT id FROM payments.merchants WHERE dataset_id=%s ORDER BY id LIMIT 2',(dataset,)).fetchall()
        ma,mb=[m['id'] for m in merchants]
        ua,ub,admin,unassigned=[uuid4() for _ in range(4)]
        ca,cb=uuid4(),uuid4()
        for uid in [ua,ub,admin,unassigned]:
            connection.execute("INSERT INTO auth.users(id,aud,role) VALUES(%s,'authenticated','authenticated')",(uid,))
        connection.execute('INSERT INTO payments.customers(id,name) VALUES(%s,%s),(%s,%s)',(ca,'test-'+str(ca),cb,'test-'+str(cb)))
        connection.execute("INSERT INTO payments.user_access(user_id,role,customer_id) VALUES(%s,'customer',%s),(%s,'customer',%s),(%s,'pine_admin',NULL)",(ua,ca,ub,cb,admin))
        connection.execute('INSERT INTO payments.customer_merchants(customer_id,dataset_id,merchant_id) VALUES(%s,%s,%s),(%s,%s,%s)',(ca,dataset,ma,cb,dataset,mb))
        def identity(uid,scope=None):
            connection.execute("SELECT set_config('request.jwt.claim.sub',%s,true),set_config('request.jwt.claims',%s,true),set_config('app.public_workspace','false',true),set_config('app.data_customer_id',%s,true)",
                               (str(uid),json.dumps({'sub':str(uid),'role':'pine_admin'}),str(scope or '')))
        def allowed(merchant):
            return connection.execute('SELECT app_private.can_read_merchant(%s,%s) AS allowed',(dataset,merchant)).fetchone()['allowed']
        identity(ua)
        assert allowed(ma) and not allowed(mb)
        profile=connection.execute('SELECT app_private.access_profile() AS p').fetchone()['p']
        assert profile['role']=='customer' and profile['customer_id']==str(ca)
        identity(ua,scope=cb)
        assert allowed(ma) and not allowed(mb) # forged admin JWT/custom scope does not elevate membership
        identity(ub)
        assert allowed(mb) and not allowed(ma)
        identity(admin)
        assert allowed(ma) and allowed(mb)
        identity(admin,scope=ca)
        assert allowed(ma) and not allowed(mb)
        identity(unassigned)
        assert not allowed(ma) and not allowed(mb)
        connection.execute('UPDATE payments.user_access SET enabled=false WHERE user_id=%s',(ua,))
        identity(ua)
        assert not allowed(ma)
        policies=connection.execute("SELECT policyname FROM pg_policies WHERE schemaname='payments'").fetchall()
        assert not any(p['policyname'].startswith(('public_','runtime_read_','research_','owned_')) for p in policies)
    finally:
        connection.rollback();connection.close()


def test_live_existing_runtime_role_denies_private_and_preserves_demo(monkeypatch):
    if os.environ.get('RUN_SUPABASE_RBAC_TESTS')!='1':
        pytest.skip('Opt-in Supabase restricted-role checks; no fixtures or grants.')
    from payments_analyst.repository import runtime_transaction
    from payments_analyst.research import research_transaction
    monkeypatch.setenv('DATA_ACCESS_MODE','public_demo')
    with runtime_transaction(None) as connection:
        total=connection.execute('SELECT count(*) AS n FROM payments.orders').fetchone()['n']
        assert total>0
        assert connection.execute("SELECT has_table_privilege(current_user,'payments.user_access','INSERT') AS allowed").fetchone()['allowed'] is False
    monkeypatch.setenv('DATA_ACCESS_MODE','rbac')
    for user in [None,uuid4()]:
        with runtime_transaction(user) as connection:
            assert connection.execute('SELECT count(*) AS n FROM payments.order_facts').fetchone()['n']==0
            for table in ['analysis_runs','scenarios','action_plans','action_simulations','query_evaluation_runs']:
                assert connection.execute(f'SELECT count(*) AS n FROM payments.{table}').fetchone()['n']==0
    with research_identity(uuid4()),research_transaction() as connection:
        assert connection.execute('SELECT count(*) AS n FROM payments.orders').fetchone()['n']==0
        permissions=connection.execute("SELECT current_setting('transaction_read_only') AS mode,has_table_privilege(current_user,'payments.analysis_runs','INSERT') AS write").fetchone()
        assert permissions=={'mode':'on','write':False}
