"""Refresh classification if a legacy Pi file worker re-advertises its offers."""
import json
import os
from biomerte.worker import Remote

home='/home/pi/.local/share/biomerte-file-storage'
def load(path):
    with open(path) as f:return json.load(f)
def main():
    cfg=load(home+'/manifest.json');node=load(home+'/node.json')
    controller=load('/home/pi/.local/share/biomerte/node.json')
    target=load(home+'/replica-target.json')
    read=Remote(cfg['url'],cfg['site'],controller['node'],None,token=controller['admin_token'])
    owner=Remote(cfg['url'],cfg['site'],node['node'],home+'/node.key')
    wanted=dict(tier='sacred_primary',reserved_bytes=2*1024**3,replica_resource=target['resource'],
                replication='asynchronous_verified_snapshots',replica_status_contract='storage.sacred.status:1')
    prefix='Sacred Pi primary (2 GiB). The Mac retains verified snapshots; check storage.sacred.status:1 for freshness. '
    changed=0
    for o in read.call('cap.discover',{})['offers']:
        if o['provider']!=node['node'] or o['resource']!=cfg['offers'][0]['resource']:continue
        before=o['usage'].get('before','')
        if all(o.get('configuration',{}).get(k)==v for k,v in wanted.items()) and before.startswith(prefix):continue
        guide={k:v for k,v in o['usage'].items() if k in ('parameters','example_inputs','before','read_result','errors')}
        if o['usage'].get('inputs_kind')=='example':guide['example_inputs']=o['usage']['calls'][2]['arguments']['inputs']
        if not before.startswith(prefix):guide['before']=prefix+before
        config=dict(o.get('configuration',{}));config.update(wanted)
        owner.call('cap.offer',dict(provider=node['node'],resource=o['resource'],contract=o['contract'],service=o['service'],
                                    state=o['state'],capacity=o['capacity'],configuration=config,usage=guide))
        changed+=1
    print('Sacred file offers updated:',changed)
if __name__=='__main__':main()
