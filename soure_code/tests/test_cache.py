"""Mock Upstash wire protocol, expiry/fallback, and authorization-before-cache tests."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from payments_analyst.cache import UpstashCache
from payments_analyst.contracts import AnalysisRequest, ResourceNotFound
from payments_analyst.repository import SupabaseRepository
from payments_analyst.queries import VERSION_SQL, DIAGNOSTICS_SQL, DAILY_SQL
from payments_analyst.sql_analyst import SCOPED_DATA_SQL
from payments_analyst.preview_routes import fixture_snapshot


class FakeRedis:
    def __init__(self):self.values={};self.calls=[]
    def handle(self,request):
        assert request.method=='POST' and str(request.url)=='https://demo.upstash.io'
        assert request.headers['Authorization']=='Bearer private-test-token'
        command=json.loads(request.content);self.calls.append(command)
        op,key,*args=command
        if op=='GET':result=self.values.get(key)
        elif op=='SET':
            assert args[-2:]==['EX',120]
            self.values[key]=args[0];result='OK'
        elif op=='DEL':result=int(self.values.pop(key,None) is not None)
        else:raise AssertionError('Unexpected Redis command')
        return httpx.Response(200,json={'result':result})
    def cache(self):
        return UpstashCache('https://demo.upstash.io','private-test-token',enabled=True,transport=httpx.MockTransport(self.handle))


def test_wire_protocol_roundtrip_preserves_money_and_dates():
    redis=FakeRedis();cache=redis.cache();key=cache.key({'scope':'customer-a','sql':'private SQL'})
    value={'money':Decimal('99999999999999999.01'),'date':date(2026,11,21),'when':datetime.now(timezone.utc)}
    assert 'private SQL' not in key and cache.read(key).status=='MISS'
    assert cache.write(key,value)
    read=cache.read(key);assert read.status=='HIT' and read.value==value
    assert redis.calls[1][-2:]==['EX',120]


@pytest.mark.parametrize('url',['http://demo.upstash.io','https://upstash.io.evil.example','https://user:password@demo.upstash.io',
 'https://demo.upstash.io?token=secret','https://demo.upstash.io/path','https://demo.upstash.io:444'])
def test_only_upstash_https_endpoints_receive_tokens(url):
    cache=UpstashCache(url,'secret',enabled=True)
    assert not cache.active and cache.read('key').status=='NOT_CONFIGURED'


def test_disabled_missing_and_malformed_configuration_bypass(monkeypatch):
    assert UpstashCache().status()['state']=='DISABLED'
    assert UpstashCache(enabled=True).status()['state']=='NOT_CONFIGURED'
    monkeypatch.setenv('UPSTASH_CACHE_TTL_SECONDS','invalid')
    assert not UpstashCache.from_environment().active
    assert not UpstashCache('https://demo.upstash.io','token',enabled=True,ttl=0).configured


def test_unavailable_redis_falls_back_and_opens_short_circuit(caplog):
    calls=[]
    def handler(request):calls.append(request);raise httpx.ReadTimeout('private-test-token',request=request)
    cache=UpstashCache('https://demo.upstash.io','private-test-token',enabled=True,transport=httpx.MockTransport(handler))
    assert cache.read('key').status=='UNAVAILABLE'
    assert cache.read('key').status=='DEGRADED'
    assert cache.write('key',{'value':1}) is False and len(calls)==1
    assert 'private-test-token' not in caplog.text


@pytest.mark.parametrize('value',['broken JSON','{"version":"wrong"}',123, '{"expires_at":NaN}'])
def test_corrupt_values_are_misses(value):
    redis=FakeRedis();cache=redis.cache();redis.values['key']=value
    assert cache.read('key').status=='INVALID'


def test_local_expiry_rejects_stale_values_even_before_redis_removes_them(monkeypatch):
    from payments_analyst import cache as module
    redis=FakeRedis();cache=redis.cache();monkeypatch.setattr(module,'time',lambda:1000)
    cache.write('key',{'count':5});monkeypatch.setattr(module,'time',lambda:1121)
    assert cache.read('key').status=='INVALID'


def test_oversize_or_nonfinite_data_not_written():
    redis=FakeRedis();cache=redis.cache();cache.max_bytes=1024
    assert cache.write('key',{'large':'x'*3000}) is False
    assert cache.write('key',{'value':Decimal('NaN')}) is False
    assert not redis.calls


def test_probe_uses_one_expiring_namespaced_key_and_no_flush():
    redis=FakeRedis();cache=redis.cache();result=cache.check()
    assert result['connection_verified'] is True and not redis.values
    assert [c[0] for c in redis.calls]==['SET','GET','DEL']
    assert all(c[1].startswith('payscope:dev:v1:') for c in redis.calls)
    assert 'private-test-token' not in str(result)


class Cursor:
    def __init__(self,rows):
        self.rows=rows;self.description=[SimpleNamespace(name=k) for k in rows[0]] if rows else []
    def fetchall(self):return deepcopy(self.rows)
    def fetchone(self):return deepcopy(self.rows[0]) if self.rows else None


class Database:
    def __init__(self):
        self.snapshot=deepcopy(fixture_snapshot('incidents'));self.allowed=True;self.merchants=['merchant-a']
        self.role='customer';self.sql_calls=0;self.daily_calls=0;self.diagnostic_calls=0
    def execute(self,query,parameters=None):
        if query==VERSION_SQL:return Cursor([self.snapshot.version] if self.allowed else [])
        if 'app_private.access_profile() AS profile' in query:
            return Cursor([{'profile':{'role':self.role},'merchants':self.merchants}])
        if query==DIAGNOSTICS_SQL:
            self.diagnostic_calls+=1;return Cursor([self.snapshot.diagnostics])
        if query==DAILY_SQL:
            self.daily_calls+=1;return Cursor(self.snapshot.daily)
        if query.startswith(SCOPED_DATA_SQL):
            self.sql_calls+=1;return Cursor([{'total_orders':50}])
        assert query.startswith('SET LOCAL search_path')
        return Cursor([])


@pytest.fixture
def repository(monkeypatch):
    from payments_analyst import repository as module
    database=Database();redis=FakeRedis()
    @contextmanager
    def transaction(user_id,**kwargs):yield database
    monkeypatch.setattr(module,'runtime_transaction',transaction)
    monkeypatch.setenv('DATA_ACCESS_MODE','rbac')
    return SupabaseRepository(redis.cache()),database,redis,AnalysisRequest(dataset_id=database.snapshot.version['id'])


def test_sql_hit_reuses_result_with_honest_evidence(repository):
    repo,database,redis,request=repository;user=uuid4();query='SELECT sum(total_orders) AS total_orders FROM data'
    first=repo.execute_generated(user,request,query);second=repo.execute_generated(user,request,query)
    assert database.sql_calls==1 and first['rows']==second['rows']
    assert first['evidence']['execution']=='EXECUTED' and second['evidence']['execution']=='CACHE_HIT'
    assert second['evidence']['executed_at']==first['evidence']['executed_at']
    assert 'cache_retrieved_at' in second['evidence']


def test_revoked_access_is_checked_before_any_redis_read(repository):
    repo,database,redis,request=repository;user=uuid4();query='SELECT sum(total_orders) AS total_orders FROM data'
    repo.execute_generated(user,request,query);calls=len(redis.calls);database.allowed=False
    with pytest.raises(ResourceNotFound):repo.execute_generated(user,request,query)
    assert len(redis.calls)==calls


def test_cache_keys_separate_users_roles_filters_fingerprints_and_scopes(repository,monkeypatch):
    from payments_analyst.access import customer_scope
    repo,database,redis,request=repository;user=uuid4();query='SELECT sum(total_orders) AS total_orders FROM data'
    repo.execute_generated(user,request,query)
    repo.execute_generated(uuid4(),request,query)
    database.role='pine_admin';repo.execute_generated(user,request,query)
    database.merchants=['merchant-b'];repo.execute_generated(user,request,query)
    database.snapshot.version['fingerprint']='b'*64;repo.execute_generated(user,request,query)
    filtered=request.model_copy(update={'payment_methods':['UPI']});repo.execute_generated(user,filtered,query)
    with customer_scope(uuid4()):repo.execute_generated(user,filtered,query)
    monkeypatch.setenv('DATA_ACCESS_MODE','public_demo');repo.execute_generated(user,filtered,query)
    assert database.sql_calls==8
    assert len({c[1] for c in redis.calls if c[0]=='SET'})==8


def test_snapshot_hit_rechecks_live_reconciliation_and_restores_dates(repository):
    from payments_analyst.analytics import analyse
    repo,database,redis,request=repository
    first=repo.load_snapshot(None,request);second=repo.load_snapshot(None,request)
    assert first.daily==second.daily and database.daily_calls==1 and database.diagnostic_calls==2
    assert second.query_evidence[-1]['execution']=='CACHE_HIT'
    assert analyse(first,request)['segments']==analyse(second,request)['segments']
    database.snapshot.diagnostics['order_count']+=1
    repo.load_snapshot(None,request)
    assert database.daily_calls==2 # Changed source quality cannot reuse the former aggregates.


def test_invalid_cached_result_does_not_hide_sql_execution(repository):
    repo,database,redis,request=repository;user=uuid4();query='SELECT sum(total_orders) AS total_orders FROM data'
    repo.execute_generated(user,request,query)
    key=next(iter(redis.values));envelope=json.loads(redis.values[key]);envelope['value']['columns']=[[]]
    redis.values[key]=json.dumps(envelope)
    result=repo.execute_generated(user,request,query)
    assert database.sql_calls==2 and result['evidence']['execution']=='EXECUTED'


def test_redis_outage_still_executes_supabase_query(repository):
    repo,database,redis,request=repository
    repo.cache=UpstashCache('https://demo.upstash.io','token',enabled=True,transport=httpx.MockTransport(lambda request:httpx.Response(503)))
    result=repo.execute_generated(None,request,'SELECT sum(total_orders) AS total_orders FROM data')
    assert database.sql_calls==1 and result['rows']==[{'total_orders':50}]
