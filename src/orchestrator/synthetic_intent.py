"""Connected intent coordinator for synthetic fixtures, not a live account client."""
from .synthetic_delegation import digest

class SyntheticIntentInteraction:
    def __init__(self, engine):self.engine=engine

    def start(self, principal, mechanism, mode='visual'):
        if mechanism not in {'oauth','session','vault','device-only'}:raise ValueError('unknown synthetic connection')
        person=self.engine._person(principal,'delegation.execute')
        with self.engine.store.transaction():
            state=self.engine.store.read()
            for gid,grant in state['grants'].items():
                if (grant['person']==person and grant['state']=='active'
                        and grant['spec']==dict(provider=mechanism,account='synthetic-account-a',purpose='source.review',actions=['source.read'],recipient='self',terms='policy-v1')):
                    try:self.engine._allowed(grant,'source.read','self')
                    except PermissionError:continue
                    tid=self.engine.submit(principal,gid,'source.read','self')
                    break
            else:tid=None
        if tid is not None:
            self.engine.run(principal,tid)
            return self.current(principal,'tasks',tid,mode)
        gid=self.engine.propose(principal,provider=mechanism,account='synthetic-account-a',purpose='source.review',
            actions=['source.read'],recipient='self',terms='policy-v1',lifetime=3600)
        return self.current(principal,'grants',gid,mode)

    def current(self, principal, collection, resource, mode='visual'):
        if mode not in {'visual','screenless'} or collection not in {'grants','tasks'}:raise ValueError('invalid interaction')
        frame=self.engine.frame(principal,collection,resource,visual=mode=='visual',audio=True)
        return dict(frame,collection=collection)

    def reply(self, principal, presented, command, mode='visual'):
        if mode not in {'visual','screenless'}:raise ValueError('invalid modality')
        collection=presented['collection'];resource=presented['resource'];revision=presented['revision'];binding=presented.get('binding')
        if command=='verify':
            return self._verify(principal,presented,mode)
        result=self.engine.respond(principal,collection,resource,revision,command,binding)
        if command=='repair':
            return self._challenge(principal,result,mode)
        if collection=='grants' and command=='confirm':
            resource=self.engine.submit(principal,resource,'source.read','self');collection='tasks'
            self.engine.run(principal,resource)
        elif collection=='tasks' and command in {'confirm','resume'}:
            self.engine.run(principal,resource)
        return self.current(principal,collection,resource,mode)

    def _challenge(self, principal, result, mode):
        available=mode in result['channels']
        description=('Complete the simulated source verification. No real account or credential is involved.' if available else
                     'The provider requires device verification. No nonvisual alternative is qualified. You can pause and retain progress.')
        choices=['verify','not_now'] if available else ['not_now']
        content=dict(description=description,terms=None,choices=choices)
        return dict(schema='intent-experience.v1',collection='tasks',resource=result['task_id'],revision=result['revision'],
            binding=result['binding'],repair=result['ticket'],state='repair_confirmation' if available else 'verification_unavailable',
            visual=content if mode=='visual' else None,audio_text=description+' Choices: '+', '.join(c.replace('_',' ') for c in choices))

    def _verify(self, principal, presented, mode):
        # Re-evaluate the original exact task/repair binding; never trust presented prose or channels.
        if presented['collection']!='tasks':raise PermissionError('verification task required')
        result=self.engine.respond(principal,'tasks',presented['resource'],presented['revision'],'repair',presented.get('binding'))
        if digest(presented.get('repair'))!=digest(result['ticket']):raise PermissionError('challenge changed')
        outcome=self.engine.complete_repair(principal,result['ticket'],channel=mode)
        if outcome!='repaired':return self._challenge(principal,result,mode)
        self.engine.run(principal,presented['resource'])
        return self.current(principal,'tasks',presented['resource'],mode)
