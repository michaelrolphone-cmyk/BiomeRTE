"""A bounded, explicit BPMN 2.0 executable profile with JSON data bindings."""
import json
import re
import xml.etree.ElementTree as ET
from .store import Fault

B = '{http://www.omg.org/spec/BPMN/20100524/MODEL}'
X = '{urn:biomerte:v1}'


def compile_xml(xml):
    if len(xml) > 512000 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise Fault('Unsupported XML declaration or size')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise Fault('Malformed BPMN: ' + str(e))
    processes = [root] if root.tag == B + 'process' else root.findall(B + 'process')
    if len(processes) != 1:
        raise Fault('Supply exactly one process; import called processes first')
    p = processes[0]
    elems, outgoing, incoming = {}, {}, {}
    for e in p:
        if e.tag == B + 'documentation':
            continue
        key = e.get('id')
        if not key or key in elems:
            raise Fault('Missing or duplicate BPMN ID')
        elems[key] = e
        if e.tag == B + 'sequenceFlow':
            outgoing.setdefault(e.get('sourceRef'), []).append(e)
            incoming.setdefault(e.get('targetRef'), []).append(e)
    nodes = {}
    starts = []
    for key, e in elems.items():
        tag = e.tag[len(B):] if e.tag.startswith(B) else ''
        if tag == 'sequenceFlow':
            if e.get('sourceRef') not in elems or e.get('targetRef') not in elems:
                raise Fault('Dangling sequence flow')
            continue
        supported_children = {B + 'incoming', B + 'outgoing', B + 'documentation'}
        if tag == 'intermediateCatchEvent':
            supported_children.update([B + 'timerEventDefinition', B + 'messageEventDefinition'])
        if any(child.tag not in supported_children for child in e):
            raise Fault('Unsupported nested BPMN behavior at ' + key)
        flows = outgoing.get(key, [])
        if tag != 'exclusiveGateway' and any(f.find(B + 'conditionExpression') is not None for f in flows):
            raise Fault('Conditions must be on an exclusive gateway')
        n = {}
        if tag == 'startEvent':
            starts.append(key)
            n['type'] = 'start'
        elif tag == 'endEvent':
            n.update(type='end', result=json.loads(e.get(X + 'result', '{}')))
        elif tag == 'serviceTask' and e.get(X + 'delegate'):
            n.update(type='delegate', to=e.get(X + 'delegate'), request=e.get(X + 'request', e.get('name', key)),
                     context=json.loads(e.get(X + 'context', '{}')), outputs=json.loads(e.get(X + 'outputs', '{}')))
        elif tag == 'serviceTask':
            n.update(type='service', args=json.loads(e.get(X + 'args', '{}')))
            if e.get(X + 'authorization'):
                n['authorization'] = json.loads(e.get(X + 'authorization'))
            contract_ref = e.get(X + 'contractRef')
            if contract_ref:
                if e.get(X + 'operation') or e.get(X + 'contract'):
                    raise Fault('Choose one service binding at ' + key)
                n['contract'] = {'$': contract_ref}
            elif e.get(X + 'contract'):
                n['contract'] = e.get(X + 'contract')
            else:
                n['operation'] = e.get(X + 'operation')
            if e.get(X + 'resourceRef'):
                n['resource'] = {'$': e.get(X + 'resourceRef')}
            elif e.get(X + 'resource'):
                n['resource'] = e.get(X + 'resource')
            if e.get(X + 'leaseScope'):
                n['lease_scope'] = e.get(X + 'leaseScope')
            if e.get(X + 'captureOutcome') == 'true':
                n['capture_outcome'] = True
            if e.get(X + 'successEqualsRef'):
                n['success_equals'] = {'$': e.get(X + 'successEqualsRef')}
        elif tag in ('userTask', 'manualTask', 'receiveTask'):
            n.update(type='message' if tag == 'receiveTask' else 'human', name=e.get('name', key),
                     question=e.get(X + 'question', e.get('name', key)), outputs=json.loads(e.get(X + 'outputs', '{}')))
        elif tag == 'task' and e.get(X + 'set'):
            n.update(type='set', values=json.loads(e.get(X + 'set')))
        elif tag == 'callActivity':
            n.update(type='call', process=e.get('calledElement'),
                     args=json.loads(e.get(X + 'args', '{}')))
        elif tag == 'intermediateCatchEvent':
            timer = e.find(B + 'timerEventDefinition/' + B + 'timeDuration')
            if timer is not None:
                m = re.match(r'^PT(\d+(?:\.\d+)?)([HMS])$', timer.text or '')
                if not m:
                    raise Fault('Supported durations: PTnH, PTnM, PTnS')
                n.update(type='timer', seconds=float(m.group(1)) * \
                         {'H': 3600, 'M': 60, 'S': 1}[m.group(2)])
            elif e.find(B + 'messageEventDefinition') is not None:
                n.update(type='message', name=e.get('name', key),
                         outputs=json.loads(e.get(X + 'outputs', '{}')))
            else:
                raise Fault('Unsupported catch event')
        elif tag == 'exclusiveGateway':
            if len(flows) == 1:
                n['type'] = 'start'
            else:
                n.update(type='choose', cases=[])
                for f in flows:
                    if f.get('id') == e.get('default'):
                        n['default'] = f.get('targetRef')
                    else:
                        cond = f.find(B + 'conditionExpression')
                        if cond is None:
                            raise Fault('Exclusive branch needs JSON condition or explicit default')
                        n['cases'].append({'when': json.loads(cond.text), 'to': f.get('targetRef')})
                if 'default' not in n:
                    raise Fault('Exclusive gateway needs default flow')
        elif tag == 'parallelGateway':
            if len(flows) > 1 and len(incoming.get(key, [])) == 1:
                n.update(type='fork', branches=[f.get('targetRef') for f in flows])
            elif len(flows) == 1 and len(incoming.get(key, [])) > 1:
                n['type'] = 'join'
            else:
                raise Fault('Use separate paired parallel split and join gateways')
        else:
            raise Fault('Unsupported BPMN element: ' + tag)
        if tag not in ('intermediateCatchEvent',) and any(c.tag.endswith('EventDefinition') for c in e):
            raise Fault('Unsupported event definition on ' + key)
        if n['type'] not in ('end', 'choose', 'fork'):
            if len(flows) != 1:
                raise Fault('Expected one outgoing flow: ' + key)
            n['next'] = flows[0].get('targetRef')
        if e.get(X + 'save'):
            n['save'] = e.get(X + 'save')
        if e.get(X + 'onError'):
            n['on_error'] = e.get(X + 'onError')
        nodes[key] = n
    if len(starts) != 1:
        raise Fault('Exactly one start event is supported')
    return {'start': starts[0], 'nodes': nodes, 'inputs': json.loads(p.get(X + 'inputs', '{}')),
            'outputs': json.loads(p.get(X + 'outputs', '{}'))}
