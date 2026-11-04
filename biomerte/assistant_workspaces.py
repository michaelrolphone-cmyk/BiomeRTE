"""Prepared integration with deployed workspace and Git contracts.

Every external effect uses the existing authorization/capability receipt path.
Workspace identity is owned by the independent episode run, never a short cycle.
"""
import base64, copy, hashlib, json, re
from pathlib import Path
from urllib.parse import urlsplit
from .store import Fault, digest
from .api import invoke

ROOT = Path(__file__).resolve().parent
SCHEMAS = json.loads((ROOT / 'workspace-contracts.json').read_text())
RUNNER = (ROOT / 'assistant_workspace_runner.py').read_text()

def owner_run(f):
    return f.get('workspace_run_id') or f['_runtime']['run_id']

def bind_owner(tx,ctx,f):
    current=ctx.workflow['run_id']; row=tx.get('runs',current)
    while row and row.get('parent') and not row['parent'].startswith('action:'):
        current=row['parent'];row=tx.need('runs',current)
    if f.get('workspace') and f.get('workspace_run_id') and f['workspace_run_id']!=current:
        raise Fault('Workspace owner is not the current root episode')
    f['workspace_run_id']=current
    return f

def decode(tx, ctx, raw):
    if raw.get('ok') is False: raise Fault('Provider business result failed')
    data = raw.get('result', raw)
    if 'artifact' in data:
        manifest = data['artifact']; parts = []
        for chunk in manifest['chunks']:
            value = invoke(tx, ctx, 'artifact.get', {'sha256': chunk['sha256']})
            part = base64.b64decode(value['data'], validate=True)
            if len(part) != chunk['size'] or hashlib.sha256(part).hexdigest() != chunk['sha256']: raise Fault('Artifact chunk mismatch')
            parts.append(part)
        encoded = b''.join(parts)
        if len(encoded) != manifest['size'] or hashlib.sha256(encoded).hexdigest() != manifest['sha256']: raise Fault('Artifact digest mismatch')
        data = json.loads(encoded)
    return data

def descriptor(f):
    w = f.get('workspace', {})
    if not all(k in w for k in ('id','resource','run_id','generation','state')): raise Fault('Workspace receipt required')
    if w['run_id'] != owner_run(f): raise Fault('Workspace belongs to a different episode run')
    return w

def access(f):
    w = descriptor(f)
    if w['state'] != 'active' or not w.get('holder'): raise Fault('Acquire the workspace before use')
    return {k: w[v] for k,v in [('workspace_id','id'),('holder','holder'),('generation','generation')]}

def action(f, operation, arguments):
    from . import assistant_support as a
    a.validate_response(arguments, SCHEMAS[operation])
    return {'contract':'workspace.'+operation+':1','resource':descriptor(f)['resource'],'inputs':{'arguments':arguments}}

def guarded_edit(f, proposed):
    if proposed['contract'] not in ('workspace.exec:1','workspace.git:1','workspace.file.write:1'): raise Fault('Coding edits must use the run workspace')
    expected = access(f); given = proposed['inputs'].get('arguments', {})
    if proposed['resource'] != descriptor(f)['resource'] or any(given.get(k) != v for k,v in expected.items()): raise Fault('Stale workspace generation or holder')
    return proposed

def runner_action(f, mode='test'):
    recipe = f.get('build_test_recipe', {})
    spec = {'root':'/workspace/source', 'mode':mode,'environment':{k:descriptor(f).get(k) for k in ('id','run_id','resource','generation','template','template_revision','image','platform')}}
    if mode == 'test':
        if not recipe.get('commands'): raise Fault('Explicit build/test recipe required')
        spec['commands'] = recipe['commands']
    elif mode == 'commit':
        if not f.get('tests_passed') or f.get('review', {}).get('recommendation') != 'sign_off' or not (f.get('tested_revision') == f.get('candidate_revision') == f.get('review', {}).get('revision')): raise Fault('Exact tested/reviewed revision required')
        spec.update(expected_revision=f['tested_revision'],message=f['source_request'].get('commit_message','Biome workflow change'))
    return action(f,'exec',dict(access(f),argv=['python3','-c',RUNNER,json.dumps(spec,separators=(',',':'))],cwd='/workspace/source'))

def save_workspace(f, data, resource):
    if data.get('run_id') != owner_run(f): raise Fault('Workspace run mismatch')
    old = f.get('workspace')
    if old and old['id'] != data.get('id'): raise Fault('Workspace identity changed')
    f['workspace'] = dict(data,resource=resource)
    f['workspace_context'] = {k:f['workspace'].get(k) for k in ('id','run_id','resource','holder','generation','state','template','template_revision','platform','checkpoint')}

def execute(tx,ctx,f,key,act):
    from . import assistant_support as a
    # Preserve the exact descriptor while a receipt or authorization is pending.
    previous = f.get('_operations',{}).get(key)
    if previous: act = previous['action']
    result = a.capability(tx,ctx,f,key,act,'Perform '+key+' for this workflow workspace')
    if result['disposition'] != 'completed': return result, None
    data = decode(tx,ctx,result['data'])
    if data.get('exit_code',0) != 0: return a.outcome(f,'faulted','workspace_command_failed',data),None
    return result,data

def source_request(f,cfg):
    s = f.get('source_request',{})
    if not all(s.get(k) for k in ('url','project','base_commit','branch')): raise Fault('Pinned source URL/project/base_commit/branch required')
    url = urlsplit(s['url']); canonical=urlsplit(cfg['gitlab_url'])
    if url.scheme != 'https' or url.netloc.lower() != canonical.netloc.lower() or url.username or url.password or url.query or url.fragment: raise Fault('Use the canonical credential-free GitLab URL')
    if url.path.rstrip('/') != '/'+s['project']+'.git': raise Fault('GitLab project and URL differ')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',s['base_commit']): raise Fault('Pin a full commit, not a moving branch')
    if not re.fullmatch(r'biome/[A-Za-z0-9_-]+',s['branch']): raise Fault('Use a dedicated biome/<name> output branch')
    return s

def hold_unresolved(result):
    """Keep recoverable episode state alive so the deployed monitor cannot delete it.

    A receipt recheck never creates a new operation. Explicit cancellation remains
    the native terminal path; it permits disposal. Operators reconcile provider
    receipts before resuming uncertain effects, not by clearing operation IDs.
    """
    f=result.get('frame',{})
    if f.get('workspace') and result.get('disposition') in ('unresolved','faulted','deferred'):
        f['workspace_blocked_outcome']={k:copy.deepcopy(v) for k,v in result.items() if k!='frame'}
        result['continuation']=dict(result.get('continuation',{}),kind='workspace_resolution',workspace_id=f['workspace']['id'],run_id=f['_runtime']['run_id'])
        result['disposition']='pending'
        result['result_code']='workspace_resolution_required'
    elif result.get('disposition')=='completed':f.pop('workspace_blocked_outcome',None)
    return result

def stage(tx,ctx,f,p,s):
    from . import assistant_support as a
    cfg=a.configuration(tx); key=p+'.'+s
    request=f.get('workspace_request',{})
    act=None; workspace_result=False
    if p == 'workspace_open':
        source=source_request(f,cfg)
        if s=='provision':
            resource=cfg['resources'].get('workspace')
            if not resource: raise Fault('Explicit Mac workspace provider binding required')
            if not any(o['resource']==resource and o['contract']=='workspace.provision:1' and o['state']=='ready' for o in tx.rows('offers')):
                return a.outcome(f,'pending','workspace_provider_unavailable',continuation={'resource':resource,'contract':'workspace.provision:1'})
            template=request.get('template',cfg['workspace_template'])
            if template not in cfg['workspace_templates']: raise Fault('Template is not qualified for this provider')
            args={'run_id':owner_run(f),'name':request.get('name','source'),'template':template,'gitlab_project':source['project'],'auto_suspend':True,'resources':request.get('resources',{})}
            act={'contract':'workspace.provision:1','resource':resource,'inputs':{'arguments':args}};workspace_result=True
        elif s=='acquire':
            act=action(f,'acquire',{'workspace_id':descriptor(f)['id'],'holder':'episode-'+owner_run(f)});workspace_result=True
        elif s=='clone':act=action(f,'git',dict(access(f),argv=['clone','--',source['url'],'/workspace/source'],cwd='/workspace'))
        elif s=='pin':act=action(f,'git',dict(access(f),argv=['checkout','-b',source['branch'],source['base_commit']],cwd='/workspace/source'))
        elif s=='snapshot':act=runner_action(f,'snapshot')
    elif p=='workspace_transfer':
        w=descriptor(f)
        if s=='handoff':
            if not f.get('workspace_to_holder'): raise Fault('Named next holder required')
            notes={'source_request':f.get('source_request'),'tested_revision':f.get('tested_revision'),'test_evidence_refs':f.get('evidence_refs',[]),'next_action':f.get('workspace_next_action'),'continuation':f.get('continuation',{})}
            act=action(f,'handoff',dict(access(f),to_holder=f['workspace_to_holder'],context=notes));workspace_result=True
    elif p=='workspace_park' or (p=='development' and s in ('park','review_park')):
        w=descriptor(f)
        if w['state'] in ('suspended','stored'): return a.outcome(f,data=f['workspace_context'])
        act=action(f,'suspend',access(f));workspace_result=True
    elif p=='workspace_resume' or (p=='development' and s=='resume'):
        w=descriptor(f)
        act=action(f,'acquire',{'workspace_id':w['id'],'holder':f.get('workspace_to_holder') or 'episode-'+owner_run(f)});workspace_result=True
    elif p=='git_publish':
        source=source_request(f,cfg)
        if s=='acquire':
            act=action(f,'acquire',{'workspace_id':descriptor(f)['id'],'holder':'episode-'+owner_run(f)});workspace_result=True
        elif s=='commit':act=runner_action(f,'commit')
        elif s=='push':
            commit=f['publication']['commit'];act=action(f,'git',dict(access(f),argv=['push','origin',commit+':refs/heads/'+source['branch']],cwd='/workspace/source'))
        elif s=='verify':act=action(f,'git',dict(access(f),argv=['ls-remote','origin','refs/heads/'+source['branch']],cwd='/workspace/source'))
        elif s=='catalog':
            if not f.get('git_mcp_request'):return a.outcome(f)
            act={'contract':'git.mcp.tools:1','resource':cfg['resources']['git'],'inputs':{'arguments':{}}}
        elif s=='review_request':
            req=f.get('git_mcp_request')
            if not req:return a.outcome(f,data=f['publication'])
            tool=next((t for t in f['git_mcp_catalog'] if t['name']==req['tool']),None)
            if not tool or digest(tool['inputSchema'])!=req['schema_digest']:raise Fault('GitLab MCP catalog/schema changed; reconcile the requested action')
            a.validate_response(req['arguments'],tool['inputSchema'])
            act={'contract':'git.mcp.call:1','resource':cfg['resources']['git'],'inputs':{'arguments':{'tool':req['tool'],'arguments':req['arguments']}}}
    if act is None: raise Fault('Unimplemented workspace stage '+key)
    result,data=execute(tx,ctx,f,key,act)
    if data is None:return result
    if workspace_result:save_workspace(f,data,act['resource'])
    elif p=='workspace_open' and s=='snapshot':
        f['source_snapshot']=json.loads(data['stdout'])
    elif p=='git_publish':
        if s=='commit':
            receipt=json.loads(data['stdout']);f['publication']={'commit':receipt['commit'],'source_revision':receipt['source_revision'],'branch':source['branch'],'url':source['url'],'verified':False}
        elif s=='verify':
            expected=f['publication']['commit']+'\trefs/heads/'+source['branch']
            if data.get('stdout','').strip()!=expected:raise Fault('Published branch does not match reviewed commit')
            f['publication']['verified']=True;f['publication']['evidence_refs']=list(f.get('evidence_refs',[]))
        elif s=='catalog':f['git_mcp_catalog']=data['tools']
        elif s=='review_request':
            if data.get('isError'):raise Fault('GitLab MCP returned a tool error')
            f['publication']['review_receipt']=data
    a.record_frame(tx,f)
    return a.outcome(f,data=data)
