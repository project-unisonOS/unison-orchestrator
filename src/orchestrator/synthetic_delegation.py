"""Synthetic connection/delegation reference. Not registered as a runtime route.

The host supplies verified PrincipalContext and a transactional encrypted state
store. Credential adapters are application-selected, never model-selected.
Only mock providers are qualified here; no live credential retrieval exists.
"""
from __future__ import annotations
import hashlib
import json
import secrets
import time
from typing import Protocol
from unison_common.principal import PrincipalContext, PrincipalKind

SCHEMA = 'synthetic-delegation.v1'
ACTIONS = {'source.read', 'source.inspect', 'report.export'}


def initial_state():
    return dict(schema=SCHEMA, connections={}, grants={}, tasks={})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CredentialUseAdapter(Protocol):
    """Broker credential use, not vault contents; outputs contain opaque handles."""
    def refresh(self, handle: str | None) -> str | None: ...
    def complete_repair(self, challenge_id: str) -> str: ...
    def execute(self, handle: str, task_id: str, action: str) -> None: ...
    def verify(self, task_id: str) -> str: ...


class MockCredentialAdapter:
    """No credentials, network, browser, partner integration or external effects."""
    def __init__(self, mechanism='oauth', refreshable=False, nonvisual=True):
        if mechanism not in {'oauth', 'persistent-session', 'password-manager'}:
            raise ValueError('unsupported mock mechanism')
        self.mechanism, self.refreshable, self.nonvisual = mechanism, refreshable, nonvisual
        self.handles, self.outcomes, self.calls = set(), {}, []
        self.repairs = 0
        self.timeout_after_effect = False
    def refresh(self, handle):
        if handle in self.handles: return handle
        if self.refreshable and handle is not None:
            new = 'opaque_' + secrets.token_urlsafe(16); self.handles.add(new); return new
        return None
    def complete_repair(self, challenge_id):
        self.repairs += 1
        handle = 'opaque_' + secrets.token_urlsafe(16); self.handles.add(handle); return handle
    def execute(self, handle, task_id, action):
        if handle not in self.handles: raise PermissionError('source authentication unavailable')
        if task_id not in self.outcomes:
            self.calls.append((task_id, action)); self.outcomes[task_id] = 'verified'
        if self.timeout_after_effect: raise TimeoutError('outcome requires verification')
    def verify(self, task_id):
        return self.outcomes.get(task_id, 'absent')


class DelegationEngine:
    def __init__(self, state_store, providers, *, enabled=False, clock=time.time):
        if not enabled: raise PermissionError('synthetic delegation disabled')
        if state_store.read().get('schema') != SCHEMA: raise PermissionError('delegation state unavailable')
        if not providers or any(not isinstance(p, MockCredentialAdapter) for p in providers.values()):
            raise PermissionError('only explicit mock providers qualified')
        self.store, self.providers, self.clock = state_store, providers, clock

    def _person(self, principal, scope):
        if (not isinstance(principal, PrincipalContext) or principal.principal_kind != PrincipalKind.PERSON
                or principal.expires_at <= self.clock() or scope not in principal.scopes
                or 'orchestrator' not in principal.audience):
            raise PermissionError('current authenticated person and scope required')
        return principal.person_id

    def _owned(self, state, collection, identifier, person):
        item = state[collection].get(identifier)
        if not item or item['person'] != person: raise PermissionError('resource unavailable')
        return item

    def propose(self, principal, *, provider, account, purpose, actions, recipient, terms, lifetime=3600):
        person = self._person(principal, 'delegation.manage')
        if (provider not in self.providers or not actions or not set(actions) <= ACTIONS
                or not all(isinstance(v,str) and 0 < len(v) <= 100 for v in (account,purpose,recipient,terms))
                or account not in {'synthetic-account-a','synthetic-account-b'}
                or purpose not in {'source.review','source.export'}
                or recipient not in {'self','synthetic-recipient'} or terms not in {'policy-v1','policy-v2'}
                or type(lifetime) is not int or not 0 < lifetime <= 86400):
            raise ValueError('bounded explicit delegation required')
        # Trusted structured proposal: no source/model text or credentials are ingested.
        spec = dict(provider=provider, account=account, purpose=purpose, actions=sorted(set(actions)), recipient=recipient, terms=terms)
        with self.store.transaction():
            state = self.store.read(); gid = 'grant_' + secrets.token_urlsafe(16)
            connection = digest([person,provider,account])
            state['connections'].setdefault(connection,dict(person=person, provider=provider, account=account,
                handle=None, repair=None, revision=0, paused=False))
            state['grants'][gid] = dict(person=person, spec=spec, fingerprint=digest(spec), connection=connection,
                revision=1, state='proposed', confirmation_expires=self.clock()+300, expires=self.clock()+lifetime)
            self.store.write(state)
        return gid

    def confirm(self, principal, gid, revision, fingerprint):
        person = self._person(principal,'delegation.manage')
        with self.store.transaction():
            state=self.store.read(); grant=self._owned(state,'grants',gid,person)
            if (grant['state']!='proposed' or revision!=grant['revision'] or fingerprint!=grant['fingerprint']
                    or grant['confirmation_expires']<=self.clock()): raise PermissionError('confirmation stale')
            grant['state']='active'; grant['revision']+=1; self.store.write(state)

    def submit(self, principal, gid, action, recipient):
        person=self._person(principal,'delegation.execute')
        with self.store.transaction():
            state=self.store.read(); grant=self._owned(state,'grants',gid,person)
            self._allowed(grant,action,recipient)
            tid='task_'+secrets.token_urlsafe(16)
            state['tasks'][tid]=dict(person=person, grant=gid, action=action, recipient=recipient,
                binding=grant['fingerprint'], revision=1, state='confirmation' if action=='report.export' else 'ready',
                approval=None, attempt=False)
            self.store.write(state)
        return tid

    def _allowed(self, grant, action, recipient):
        if (grant['state']!='active' or grant['expires']<=self.clock()
                or action not in grant['spec']['actions'] or recipient!=grant['spec']['recipient']):
            raise PermissionError('delegation no longer authorizes task')

    def confirm_task(self, principal, tid, revision, binding):
        person=self._person(principal,'delegation.execute')
        with self.store.transaction():
            state=self.store.read(); task=self._owned(state,'tasks',tid,person); grant=state['grants'][task['grant']]
            self._allowed(grant,task['action'],task['recipient'])
            if task['state']!='confirmation' or revision!=task['revision'] or binding!=self._task_binding(task,grant):
                raise PermissionError('action confirmation stale')
            task.update(approval=dict(binding=binding,expires=self.clock()+300),state='ready',revision=task['revision']+1)
            self.store.write(state)

    def _task_binding(self, task, grant):
        return digest([grant['fingerprint'],task['action'],task['recipient'],task['grant']])

    def control(self, principal, collection, identifier, command, revision):
        person=self._person(principal,'delegation.manage')
        if collection not in {'tasks','grants'} or command not in {'pause','resume','revoke','delete'}:
            raise ValueError('unsupported control')
        with self.store.transaction():
            state=self.store.read(); item=self._owned(state,collection,identifier,person)
            if revision != item['revision']: raise PermissionError('control response stale')
            if item['state']=='deleted' or (item['state']=='revoked' and command!='delete'): raise PermissionError('terminal state')
            if command in {'pause','resume'}:
                if collection=='grants':
                    if (command=='pause' and item['state']!='proposed') or (command=='resume' and item['state']!='paused'):
                        raise PermissionError('only a proposed grant can be deferred')
                    item.update(state='paused' if command=='pause' else 'proposed',revision=item['revision']+1,confirmation_expires=self.clock()+300)
                    self.store.write(state);return
                if item['state'] in {'verified','revoked','deleted'}:
                    raise PermissionError('task cannot change pause state')
                item['state']='paused' if command=='pause' else ('pending_verification' if item['attempt'] else 'ready')
            else:
                item['state']='revoked' if command=='revoke' else 'deleted'
                if collection=='grants':
                    conn=state['connections'][item['connection']]
                    conn['revision']+=1;conn['repair']=None
                    for task in state['tasks'].values():
                        if task.get('grant')==identifier:
                            task.update(state=item['state'],revision=task['revision']+1)
                            if command=='delete':
                                for key in list(task):
                                    if key not in {'person','revision','state'}:del task[key]
                    if command=='delete' and not any(g.get('connection')==item['connection'] and g is not item and g['state'] not in {'revoked','deleted'} for g in state['grants'].values()):
                        conn['handle']=None
                if command=='delete':
                    # Retain a minimal terminal tombstone. No source bytes or credentials here.
                    for key in list(item):
                        if key not in {'person','revision','state'}:del item[key]
            item['revision']+=1;self.store.write(state)

    def run(self, principal, tid):
        person=self._person(principal,'delegation.execute')
        with self.store.transaction():
            state=self.store.read(); task=self._owned(state,'tasks',tid,person)
            if task['state'] in {'paused','revoked','deleted','confirmation','verified'}: return task['state']
            grant=state['grants'][task['grant']]
            self._allowed(grant,task['action'],task['recipient'])
            if task['binding']!=grant['fingerprint']:raise PermissionError('task terms changed')
            if task['action']=='report.export' and (not task['approval'] or task['approval']['expires']<=self.clock()
                    or task['approval']['binding']!=self._task_binding(task,grant)):
                task.update(state='confirmation',revision=task['revision']+1);self.store.write(state);return 'confirmation'
            conn=state['connections'][grant['connection']]; provider=self.providers[conn['provider']]
            if task['attempt']:
                outcome=provider.verify(tid)
                if outcome=='verified':task.update(state='verified',revision=task['revision']+1);self.store.write(state);return 'verified'
                # Absence is not proof that another worker's submitted action cannot complete.
                # Never automatically re-execute an existing attempt.
                return 'pending_verification'
            handle=provider.refresh(conn['handle'])
            if handle is None:
                if conn['repair'] is None:
                    conn['revision']+=1;conn['repair']='repair_'+secrets.token_urlsafe(16)
                task.update(state='waiting_repair',revision=task['revision']+1);self.store.write(state);return 'waiting_repair'
            conn['handle']=handle
            task.update(state='pending_verification',attempt=True,revision=task['revision']+1)
            self.store.write(state)
        # Potential external effect is separated from durable pending-state commit.
        try:provider.execute(handle,tid,task['action'])
        except (TimeoutError,PermissionError):pass
        with self.store.transaction():
            state=self.store.read(); current=self._owned(state,'tasks',tid,person)
            if current['state']!='pending_verification':return current['state']
            grant=state['grants'][current['grant']]
            self._person(principal,'delegation.execute')
            self._allowed(grant,current['action'],current['recipient'])
            if provider.verify(tid)=='verified':current.update(state='verified',revision=current['revision']+1)
            self.store.write(state);return current['state']

    def repair_ticket(self, principal, gid):
        person=self._person(principal,'delegation.manage')
        with self.store.transaction():
            state=self.store.read();grant=self._owned(state,'grants',gid,person)
            if grant['state']!='active' or grant['expires']<=self.clock():raise PermissionError('delegation unavailable')
            conn=state['connections'][grant['connection']]
            return dict(connection=grant['connection'],revision=conn['revision'],challenge=conn['repair'],nonvisual=self.providers[conn['provider']].nonvisual)

    def complete_repair(self, principal, ticket, *, channel):
        person=self._person(principal,'delegation.manage')
        with self.store.transaction():
            state=self.store.read(); conn=self._owned(state,'connections',ticket['connection'],person)
            if not conn['repair'] or ticket['revision']!=conn['revision'] or ticket['challenge']!=conn['repair']:
                raise PermissionError('repair response stale')
            provider=self.providers[conn['provider']]
            if channel not in {'visual','screenless'} or (channel=='screenless' and not provider.nonvisual):
                return 'provider_device_verification_required'
            live=[g for g in state['grants'].values() if g.get('connection')==ticket['connection'] and g['state']=='active' and g['expires']>self.clock()]
            if not live:raise PermissionError('no current delegation')
            conn.update(handle=provider.complete_repair(conn['repair']),repair=None,revision=conn['revision']+1)
            self.store.write(state)
        return 'repaired'

    def frame(self, principal, collection, identifier, *, visual=True, audio=True):
        person=self._person(principal,'delegation.read')
        with self.store.transaction():
            state=self.store.read(); item=self._owned(state,collection,identifier,person)
            status=item['state'];choices=[];terms=None;binding=None
            if collection=='grants' and status=='proposed':
                terms=item['spec'];binding=item['fingerprint'];choices=['confirm','not_now']
            elif collection=='tasks' and status=='confirmation':
                grant=state['grants'][item['grant']];terms=dict(grant['spec'],action=item['action']);binding=self._task_binding(item,grant);choices=['confirm','not_now']
            elif status=='waiting_repair':choices=['repair','not_now','revoke','delete']
            elif status=='paused':choices=['resume','revoke','delete']
            elif status not in {'revoked','deleted','verified'}:choices=['stop','revoke','delete']
            description={'pending_verification':'The action may already have completed. Its outcome must be verified before retry.',
                'waiting_repair':'Your source connection needs one repair. Dependent tasks retain their progress.',
                'paused':'Paused. This does not revoke permission or undo an action.',
                'revoked':'Permission revoked. An already submitted action may have completed.',
                'deleted':'Local task details deleted. This does not undo external actions.',
                'verified':'The mock provider verified completion.',
                'confirmation':'Confirm this exact consequential mock action.',
                'proposed':'Review this source delegation separately from signing in to Unison.',
                'ready':'Ready under the current delegation.'}.get(status,'Unavailable')
            # Only controlled status prose and explicit non-secret authority terms. No raw provider/source text.
            if status=='waiting_repair':
                grant=state['grants'][item['grant']];conn=state['connections'][grant['connection']]
                if not self.providers[conn['provider']].nonvisual:
                    description += ' The provider requires device verification; no nonvisual alternative is qualified. You can pause and retain progress.'
                    if not visual: choices=['not_now','revoke','delete']
                else: description += ' A simulated nonvisual verification path is available.'
            content=dict(description=description,terms=terms,choices=choices)
            spoken=description + (' Terms: '+json.dumps(terms,sort_keys=True) if terms else '') + ' Choices: '+', '.join(choices)

            return dict(schema='intent-experience.v1',resource=identifier,revision=item['revision'],binding=binding,
                state=status,visual=content if visual else None,audio_text=spoken if audio else None,
                fallback='text_available' if visual and not audio else ('screenless_available' if audio else 'no_available_modality'))

    def respond(self, principal, collection, identifier, revision, choice, binding=None):
        if choice=='confirm':
            if collection=='grants':return self.confirm(principal,identifier,revision,binding)
            if collection=='tasks':return self.confirm_task(principal,identifier,revision,binding)
            raise ValueError('invalid confirmation resource')
        command={'not_now':'pause','stop':'pause','resume':'resume','revoke':'revoke','delete':'delete'}.get(choice)
        if not command:raise ValueError('unsupported response')
        return self.control(principal,collection,identifier,command,revision)


def visual_document(frame):
    """Semantic static fallback representation, not a connected application UI."""
    from html import escape
    content=frame['visual']
    if content is None:return None
    description=escape(content['description'])
    terms=escape(json.dumps(content['terms'],sort_keys=True)) if content['terms'] else 'No new authority requested.'
    choices=''.join('<button type="button">'+escape(choice.replace('_',' '))+'</button>' for choice in content['choices'])
    return '<!doctype html><html lang="en"><meta charset="utf-8"><title>Unison synthetic intent</title><main><h1>Your requested outcome</h1><p role="status">'+description+'</p><h2>Permission terms</h2><p>'+terms+'</p><h2>Available choices</h2>'+choices+'</main></html>'
