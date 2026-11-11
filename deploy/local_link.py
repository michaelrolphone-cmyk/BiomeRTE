"""Gateway transport for independent local SQLite controllers.

This link does not replay a shared journal or restore another controller's event
state. Every job remains pinned to its explicitly routed controller.
"""
import concurrent.futures
import json
import os
import socket
import threading
import time


def local_settings(conf):
    if conf.get('gateway_journal') or conf.get('etcd'):
        raise ValueError('Local gateway link cannot coexist with shared storage')
    settings = conf.get('gateway_link')
    if not isinstance(settings, dict) or not settings.get('controller_id'):
        raise ValueError('Local gateway link requires its registered transport identity')
    capacity = settings.get('capacity', 1)
    if type(capacity) is not int or capacity < 1:
        raise ValueError('Invalid controller capacity')
    return settings


def verify_destination(conf, settings, values):
    if values.get('controller') is not None and values['controller'] != conf['node']:
        raise ValueError('Request addressed to a different application controller')
    if values.get('controller_owner') is not None and values['controller_owner'] != settings['controller_id']:
        raise ValueError('Request addressed to a different transport controller')


def heartbeat_payload(health):
    return {'position': 0, 'ready': health.get('realms', {}).get('site') == 'ready',
            'load': os.getloadavg()[0] / (os.cpu_count() or 1), 'host': socket.gethostname()}


def session_packet_result(service, values):
    from biomerte.store import Fault
    try:
        if service is None or not hasattr(service, 'accept'):
            raise Fault('Session packet receiver unavailable', 503)
        receipt = service.accept(values)
        return {'structuredContent':receipt,
                'content':[{'type':'text','text':json.dumps(receipt)}]}
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        body = {'error':{'message':'Malformed session packet', 'status':400}}
        return {'isError':True, 'structuredContent':body,
                'content':[{'type':'text','text':json.dumps(body)}]}
    except Fault as error:
        body = {'error':{'message':str(error), 'status':error.status}}
        return {'isError':True, 'structuredContent':body,
                'content':[{'type':'text','text':json.dumps(body)}]}


def run(args, origin, token, site_access, fetch, dispatch, event_service, event_change,
        session_maintenance=None):
    with open(os.path.join(args.home, 'node.json')) as handle:
        conf = json.load(handle)
    settings = local_settings(conf)
    local = 'http://127.0.0.1:' + str(args.port)
    from persistent_link import ControllerLink
    from result_delivery import complete
    persistent = ControllerLink(origin, token, site_access,
                                os.path.join(args.home, 'gateway-transport-status.json'))
    ready = threading.Event()
    event_lock = threading.Lock()
    events_path = os.path.join(os.path.dirname(args.key), 'events.sqlite')

    def send(path, payload):
        return fetch(origin + '/bridge/' + path, payload, token, site_access=site_access)

    def heartbeat():
        while True:
            try:
                payload = heartbeat_payload(fetch(local + '/health'))
                send('ha/heartbeat', payload)
                ready.set() if payload['ready'] else ready.clear()
            except Exception:
                ready.clear()
            time.sleep(settings.get('heartbeat_interval', 2))

    def event_action(action):
        with event_lock:
            events = event_service(events_path, local, args.home, origin, token, site_access)
            try:
                return action(events)
            finally:
                events.close()

    def delivery():
        while True:
            try:
                event_action(lambda events: events.deliver() if events.active() else None)
            except Exception as error:
                print('Local event retry: ' + type(error).__name__, flush=True)
            time.sleep(2)

    def maintenance():
        while True:
            try:
                session_maintenance()
            except Exception as error:
                print('Local session reconciliation: ' + type(error).__name__, flush=True)
            time.sleep(settings.get('session_interval', 2))

    targets = [heartbeat, delivery]
    if session_maintenance is not None:
        targets.append(maintenance)
    for target in targets:
        thread = threading.Thread(target=target)
        thread.daemon = True
        thread.start()

    def process(job):
        try:
            name = job['request']['name']
            values = job['request'].get('arguments', {})
            verify_destination(conf, settings, values)
            if name == 'biome_session_packet':
                result = session_packet_result(session_maintenance, values)
            elif name == 'grok_receiver_change':
                from grok_receivers import mutate
                value = event_action(lambda events: mutate(events, values['action'], values['args'], values['owner'], values['grant']))
                result = {'content': [{'type': 'text', 'text': json.dumps(value)}]}
            elif name == 'mcp_event_change':
                result = event_action(lambda events: event_change(events, values))
            else:
                result = dispatch(job, local, conf)
            complete({'id': job['id'], 'lease': job['lease'], 'result': result},
                     send, persistent, job.get('_delivery_socket'))
        except Exception as error:
            # Completion uncertainty remains associated with this owner's request.
            print('Local command reconciliation: ' + type(error).__name__, flush=True)

    from controller_inbox import dispatch_loop
    dispatch_loop(persistent, lambda: send('next', {}), process,
                  settings.get('capacity', 1), ready.is_set,
                  lambda error: print('Local link retry: ' + type(error).__name__, flush=True))
