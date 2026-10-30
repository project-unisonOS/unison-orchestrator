"""Unit/simulation evidence only. No actual login, speech or provider challenge."""
import copy
import json
from contextlib import contextmanager
from threading import RLock
import pytest
from unison_common.principal import PrincipalContext
from orchestrator.synthetic_delegation import DelegationEngine, MockCredentialAdapter, initial_state, visual_document

class MemoryStore:
    def __init__(self, value=None):self.value=copy.deepcopy(value or initial_state());self.lock=RLock()
    def read(self):return copy.deepcopy(self.value)
    def write(self,value):self.value=copy.deepcopy(value)
    @contextmanager
    def transaction(self):
        with self.lock:yield


def principal(person='alice',scopes=('delegation.manage','delegation.execute','delegation.read')):
    return PrincipalContext(principal_id=person,principal_kind='person',person_id=person,
        assistant_instance_id='assistant',household_id='household',membership_id='member',
        key_handle='synthetic-key-handle',credential_namespace=person,data_namespace=person,
        cache_namespace=person,index_namespace=person,auth_method='synthetic-test',assurance='high',
        token_id='synthetic-token',issued_at=1,expires_at=10000,audience=('orchestrator',),scopes=scopes)

@pytest.fixture
def setup():
    store=MemoryStore();provider=MockCredentialAdapter();now=[100]
    engine=DelegationEngine(store,{'mock':provider},enabled=True,clock=lambda:now[0])
    return engine,store,provider,now


def grant(engine,**changes):
    args=dict(provider='mock',account='synthetic-account-a',purpose='source.review',actions=['source.read','report.export'],recipient='self',terms='policy-v1')
    args.update(changes);gid=engine.propose(principal(),**args)
    frame=engine.frame(principal(),'grants',gid)
    engine.confirm(principal(),gid,frame['revision'],frame['binding'])
    return gid


def repair(engine,gid,channel='screenless'):
    return engine.complete_repair(principal(),engine.repair_ticket(principal(),gid),channel=channel)


def test_five_tasks_one_repair_resume_only_authorized(setup):
    engine,store,provider,now=setup;gid=grant(engine)
    tids=[engine.submit(principal(),gid,'source.read','self') for _ in range(5)]
    for tid in tids:assert engine.run(principal(),tid)=='waiting_repair'
    assert len({engine.repair_ticket(principal(),gid)['challenge'] for _ in tids})==1
    engine.control(principal(),'tasks',tids[0],'pause',store.read()['tasks'][tids[0]]['revision'])
    engine.control(principal(),'tasks',tids[1],'revoke',store.read()['tasks'][tids[1]]['revision'])
    assert repair(engine,gid)=='repaired'
    assert [engine.run(principal(),tid) for tid in tids]==['paused','revoked','verified','verified','verified']
    assert provider.repairs==1 and len(provider.calls)==3

@pytest.mark.parametrize('field,value',[('account','synthetic-account-b'),('purpose','source.export'),('recipient','synthetic-recipient'),('terms','policy-v2'),('actions',['source.inspect'])])
def test_stale_confirmation_cannot_authorize_changed_terms(setup,field,value):
    engine,*_=setup
    args=dict(provider='mock',account='synthetic-account-a',purpose='source.review',actions=['source.read'],recipient='self',terms='policy-v1')
    first=engine.propose(principal(),**args);old=engine.frame(principal(),'grants',first)
    args[field]=value;new=engine.propose(principal(),**args)
    with pytest.raises(PermissionError):engine.confirm(principal(),new,old['revision'],old['binding'])


def test_consequential_confirmation_is_distinct_and_expires(setup):
    engine,store,provider,now=setup;gid=grant(engine);tid=engine.submit(principal(),gid,'report.export','self')
    assert engine.run(principal(),tid)=='confirmation'
    frame=engine.frame(principal(),'tasks',tid,visual=False)
    assert frame['visual'] is None and 'report.export' in frame['audio_text']
    engine.respond(principal(),'tasks',tid,frame['revision'],'confirm',frame['binding'])
    assert engine.run(principal(),tid)=='waiting_repair';repair(engine,gid)
    now[0]+=301
    assert engine.run(principal(),tid)=='confirmation' and not provider.calls


def test_not_now_mode_switch_restart_and_late_response(setup):
    engine,store,provider,now=setup;gid=grant(engine);tid=engine.submit(principal(),gid,'source.read','self')
    frame=engine.frame(principal(),'tasks',tid)
    engine.respond(principal(),'tasks',tid,frame['revision'],'not_now')
    with pytest.raises(PermissionError):engine.respond(principal(),'tasks',tid,frame['revision'],'resume')
    restarted=DelegationEngine(MemoryStore(store.read()),{'mock':provider},enabled=True,clock=lambda:now[0])
    frame=restarted.frame(principal(),'tasks',tid,visual=False)
    assert frame['state']=='paused' and frame['visual'] is None
    assert 'Paused' in frame['audio_text']
    restarted.respond(principal(),'tasks',tid,frame['revision'],'resume')
    assert restarted.run(principal(),tid)=='waiting_repair'


def test_late_repair_after_revocation_cannot_reactivate(setup):
    engine,store,provider,_=setup;gid=grant(engine);tid=engine.submit(principal(),gid,'source.read','self')
    engine.run(principal(),tid);ticket=engine.repair_ticket(principal(),gid)
    engine.control(principal(),'grants',gid,'revoke',store.read()['grants'][gid]['revision'])
    with pytest.raises(PermissionError):engine.complete_repair(principal(),ticket,channel='screenless')
    assert engine.run(principal(),tid)=='revoked' and not provider.calls
    engine.control(principal(),'grants',gid,'delete',store.read()['grants'][gid]['revision'])
    assert set(store.read()['tasks'][tid])=={'person','state','revision'}


def test_unsupported_nonvisual_challenge_preserves_progress(setup):
    engine,store,provider,_=setup;provider.nonvisual=False;gid=grant(engine);tid=engine.submit(principal(),gid,'source.read','self')
    engine.run(principal(),tid)
    assert repair(engine,gid)=='provider_device_verification_required'
    frame=engine.frame(principal(),'tasks',tid,visual=False)
    assert 'no nonvisual alternative is qualified' in frame['audio_text'] and provider.repairs==0
    assert store.read()['tasks'][tid]['state']=='waiting_repair'
    engine.respond(principal(),'tasks',tid,frame['revision'],'not_now')
    assert engine.run(principal(),tid)=='paused'

@pytest.mark.parametrize('mechanism',['oauth','persistent-session','password-manager'])
def test_refresh_and_reuse_never_exposes_opaque_handle(setup,mechanism,caplog):
    engine,store,provider,_=setup;provider.mechanism=mechanism;gid=grant(engine)
    first=engine.submit(principal(),gid,'source.read','self');engine.run(principal(),first);repair(engine,gid)
    assert engine.run(principal(),first)=='verified'
    second=engine.submit(principal(),gid,'source.read','self');assert engine.run(principal(),second)=='verified'
    provider.handles.clear();provider.refreshable=True
    third=engine.submit(principal(),gid,'source.read','self');assert engine.run(principal(),third)=='verified'
    frames=[engine.frame(principal(),'tasks',third,visual=v,audio=a) for v,a in [(True,False),(False,True),(False,False),(True,True)]]
    assert frames[0]['fallback']=='text_available' and frames[2]['fallback']=='no_available_modality'
    assert '<button' in visual_document(engine.frame(principal(),'grants',engine.propose(principal(),provider='mock',account='synthetic-account-a',purpose='source.review',actions=['source.read'],recipient='self',terms='policy-v1')))
    assert provider.repairs==1 and 'opaque_' not in json.dumps(frames)+caplog.text


def test_ambiguous_effect_verified_before_any_retry(setup):
    engine,store,provider,now=setup;gid=grant(engine);tid=engine.submit(principal(),gid,'source.read','self')
    engine.run(principal(),tid);repair(engine,gid);provider.timeout_after_effect=True
    assert engine.run(principal(),tid)=='verified'
    assert engine.run(principal(),tid)=='verified' and len(provider.calls)==1


def test_scope_person_expiry_and_default_disable(setup):
    engine,store,provider,now=setup;gid=grant(engine)
    with pytest.raises(PermissionError):engine.frame(principal('bob'),'grants',gid)
    with pytest.raises(PermissionError):engine.submit(principal(scopes=('delegation.read',)),gid,'source.read','self')
    with pytest.raises(PermissionError):DelegationEngine(store,{'mock':provider})
    tid=engine.submit(principal(),gid,'source.read','self');now[0]=4000
    with pytest.raises(PermissionError):engine.run(principal(),tid)
    now[0]=10001
    with pytest.raises(PermissionError):engine.frame(principal(),'grants',gid)

def test_inflight_attempt_never_reexecuted_when_verification_unknown(setup):
    engine,store,provider,_=setup;gid=grant(engine);tid=engine.submit(principal(),gid,'source.read','self')
    engine.run(principal(),tid);repair(engine,gid)
    state=store.read();state['tasks'][tid].update(attempt=True,state='pending_verification');store.write(state)
    assert engine.run(principal(),tid)=='pending_verification' and not provider.calls


def test_pause_proposal_does_not_grant_authority(setup):
    engine,store,provider,_=setup
    gid=engine.propose(principal(),provider='mock',account='synthetic-account-a',purpose='source.review',actions=['source.read'],recipient='self',terms='policy-v1')
    frame=engine.frame(principal(),'grants',gid)
    engine.respond(principal(),'grants',gid,frame['revision'],'not_now')
    with pytest.raises(PermissionError):engine.confirm(principal(),gid,frame['revision'],frame['binding'])
    with pytest.raises(PermissionError):engine.submit(principal(),gid,'source.read','self')


def test_concurrent_runs_produce_one_verified_effect(setup):
    from concurrent.futures import ThreadPoolExecutor
    engine,store,provider,_=setup;gid=grant(engine);tid=engine.submit(principal(),gid,'source.read','self')
    engine.run(principal(),tid);repair(engine,gid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes=list(pool.map(lambda _:engine.run(principal(),tid),range(8)))
    assert set(outcomes)<={'pending_verification','verified'}
    assert engine.run(principal(),tid)=='verified' and len(provider.calls)==1
