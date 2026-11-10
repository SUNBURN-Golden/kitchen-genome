"""Balance requests and completion requests are always mocked independently."""
import json
import pytest
from kg_ai.balance import (BalanceGate, parse_balance, http_balance, BALANCE_MAX_BYTES,
                           BALANCE_TIMEOUT_SEC, BALANCE_URL)
from kg_ai.budget import Budget
from kg_ai.deepseek import Client, DeepSeekError
from kg_collect.deepseek import Adapters
from kg_collect.targets import Gazetteer
from test_deepseek_client import conn, meter, response
from test_deepseek_collect import world, doc
from test_ai_review import env, add, rv
from tests_balance_fake import healthy_balance


def wallet(amount='1.00', currency='USD', available=True):
    return 200,json.dumps({'is_available':available,'balance_infos':[{'currency':currency,'total_balance':amount}]}).encode()


@pytest.mark.parametrize('amount,currency,expected', [
    ('0','USD','balance_low'),('0.49','USD','balance_low'),('0.50','USD',None),('1','USD',None),
    ('3.99','CNY','balance_low'),('4.00','CNY',None),('5','CNY',None),
    ('NaN','USD','balance_unavailable'),('Infinity','USD','balance_unavailable'),('-1','USD','balance_unavailable'),
    ('1','EUR','balance_unavailable'),('oops','USD','balance_unavailable'),
])
def test_balance_floors(amount,currency,expected):
    assert parse_balance(*wallet(amount,currency)) == expected


@pytest.mark.parametrize('status,body,expected', [
    (200,{'is_available':False},'balance_low'),
    (200,{'is_available':'false'},'balance_unavailable'),
    (200,{'is_available':True,'balance_infos':[]},'balance_unavailable'),
    (200,{'is_available':True,'balance_infos':[{'currency':'USD','total_balance':1}]},'balance_unavailable'),
    (200,{'is_available':True,'balance_infos':[{'currency':'USD','total_balance':'1'}]*2},'balance_unavailable'),
    (401,{},'balance_unavailable'),(429,{},'balance_unavailable'),(302,{},'balance_unavailable'),
])
def test_balance_schema(status,body,expected):
    assert parse_balance(status,json.dumps(body).encode())==expected


def test_mixed_wallets_and_body_bounds():
    body={'is_available':True,'balance_infos':[{'currency':'USD','total_balance':'.25'}, {'currency':'CNY','total_balance':'2'}]}
    assert parse_balance(200,json.dumps(body).encode()) is None
    assert parse_balance(200,b'{' )=='balance_unavailable'
    assert parse_balance(200,b'x'*(BALANCE_MAX_BYTES+1))=='balance_unavailable'


def test_ttl_latch_timeout_and_missing_key():
    now=[0]; calls=[]
    gate=BalanceGate(lambda key:calls.append(1) or healthy_balance(key),lambda:now[0])
    assert gate.check('')=='missing_key' and not calls
    assert gate.check('dummy') is None and gate.check('dummy') is None and len(calls)==1
    now[0]=60
    assert gate.check('dummy') is None and len(calls)==2
    gate.transport=lambda key:wallet('0')
    now[0]=120
    assert gate.check('dummy')=='balance_low'
    gate.transport=lambda key:pytest.fail('latched pause retried')
    now[0]=999
    assert gate.check('dummy')=='balance_low'
    def timeout(key): raise TimeoutError('never-report-provider-error')
    assert BalanceGate(timeout).check('dummy')=='balance_unavailable'


def test_completion_only_fake_cannot_open_network_or_reserve(conn,monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request,'build_opener',lambda *a:pytest.fail('unexpected network setup'))
    c=Client(Budget(meter(conn)),key='dummy',transport=lambda *a:pytest.fail('completion dispatched'))
    with pytest.raises(DeepSeekError,match='balance_unavailable'): c.call('x','JSON',{},lambda x:x)
    assert conn.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0]==0


@pytest.mark.parametrize('balance', [wallet('0'),wallet(available=False)])
def test_pause_has_no_charge_and_is_safe_to_log(conn,balance,capsys):
    c=Client(Budget(meter(conn)),key='synthetic-secret',transport=lambda *a:pytest.fail('paid call'),balance_transport=lambda key:balance)
    with pytest.raises(DeepSeekError,match='balance_low'): c.call('x','JSON',{},lambda x:x)
    assert c.status=='paused' and c.budget.meter.used==0
    assert conn.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0]==0
    assert 'synthetic-secret' not in capsys.readouterr().out


def test_validated_offline_replay_at_zero_balance(conn):
    first=Client(Budget(meter(conn)),key='dummy',transport=lambda *a:response(),balance_transport=healthy_balance)
    assert first.call('x','JSON',{},lambda x:x)=={'ok':True}
    replay=Client(Budget(meter(conn)),key='dummy',transport=lambda *a:pytest.fail('paid replay'),balance_transport=lambda *a:pytest.fail('offline cache queried balance'))
    replay.stopped='balance_low'
    assert replay.call('x','JSON',{},lambda x:x)=={'ok':True}


def test_production_get_transport_is_bounded(monkeypatch):
    calls=[]
    class Response:
        status=200
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,n):
            assert n==BALANCE_MAX_BYTES+1
            return wallet()[1]
    class Opener:
        def open(self,req,timeout):
            assert req.full_url==BALANCE_URL and req.get_method()=='GET'
            assert timeout==BALANCE_TIMEOUT_SEC and req.get_header('Authorization')=='Bearer dummy'
            calls.append(1)
            return Response()
    import urllib.request
    def builder(*handlers):
        assert handlers[0].proxies=={}
        assert handlers[1].redirect_request(None,None,302,'',{},'https://other.test') is None
        return Opener()
    monkeypatch.setattr(urllib.request,'build_opener',builder)
    assert parse_balance(*http_balance('dummy')) is None and len(calls)==1


def test_extract_balance_is_clean_skip(world):
    c=Client(Budget(meter(world)),key='dummy',transport=lambda *a:pytest.fail('paid call'),balance_transport=lambda key:wallet('0'))
    a=Adapters(c)
    assert a.extract(doc('달빛정원 운영 소식입니다.'),[],Gazetteer(world))==[]
    assert a.defer_extract and a.stats['skipped_balance']==1 and a.stats['failed']==a.stats['called']==0


def test_review_balance_keeps_queue_open(env):
    conn,seed,log,raw=env
    add(conn,'balance-x','employment_claim','한도윤 셰프가 총괄 셰프를 맡는다',chef='한도윤')
    c=Client(Budget(meter(conn)),key='dummy',transport=lambda *a:pytest.fail('paid call'),balance_transport=lambda key:wallet(available=False))
    stats=rv.review(conn,str(seed),str(log),raw,client=c)
    assert stats['reason']=='balance_low' and stats['remaining']==1 and stats['calls']==stats['rejected']==0
    assert not log.exists()
    assert conn.execute('SELECT COUNT(*) FROM ai_request').fetchone()[0]==0
