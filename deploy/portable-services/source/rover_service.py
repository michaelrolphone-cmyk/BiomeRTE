"""Mac paired Rover owner and outbound-only public ingress."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import pathlib
import signal
import sys
import tempfile
import time
import urllib.request
from urllib.error import HTTPError
import uuid
from aiohttp import web
from portable_common import read_json
from biomerte.identity import sign
from biomerte.rover import Rover
from biomerte.control_clock import ControlClock
from biomerte.service_routing import ServiceOwnerResolver
from rover_transport import Outbound, derived, local_router
from site_bridge import SiteBridge
from rover_routes import ControllerRoutes

GATEWAY='https://biomerte-controller.cantikash.chatgpt.site'
PUBLIC='https://biomerte-stream-gateway-e0322b2b60c9.herokuapp.com'


class LocalAuthority:
    """Use the running controller API; never run a bundled authority on its database."""
    def __init__(self, directory, caller_home, endpoint='http://127.0.0.1:8740/v1/call'):
        self.endpoint = endpoint
        self.config = read_json(str(pathlib.Path(directory)/'node.json'))
        self.node = self.config['node']
        self.caller_home = pathlib.Path(caller_home)
        self.identity = read_json(str(self.caller_home/'node.json'))

    def call(self, operation, arguments, request_id, auth=None, realm='site', controller=None):
        envelope = dict(site=self.config['site'], realm=realm, operation=operation,
                        arguments=arguments, id=request_id, controller=controller or self.node)
        envelope['auth'] = auth or dict(actor=self.identity['node'],
            signature=sign(str(self.caller_home/'node.key'), envelope))
        request = urllib.request.Request(self.endpoint,
            data=json.dumps(envelope).encode(), headers={'Content-Type':'application/json',
            'User-Agent':'BiomeRTE-Rover-Server/1'})
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
        if 'error' in value:
            raise ConnectionError('Controller rejected Rover request: '+str(value['error']))
        return value['result']


class RoutedRemote(ControllerRoutes):
    def __init__(self, app, home, *, resolver=None, state_home=None, gateway_url=None):
        self.gateway_url = gateway_url or GATEWAY
        self.app, self.home = app, home
        self.identity = read_json(str(home/'node.json'))
        self.vision_route = None
        self.vision_scope = None
        self.service_resolver = resolver if resolver is not None else ServiceOwnerResolver(
            PUBLIC, declarations=lambda: read_json(str(home/'node.json')).get(
                'service_authority_declarations', []))
        super().__init__(app.node, state_home or home, self.signed_request)

    def resolve_new(self, contract, resource):
        if self.vision_scope is not None and (contract, resource) == self.vision_scope[:2]:
            return self.app.node
        owner = self.service_resolver(resource)
        return owner if owner is not None else super().resolve_new(contract, resource)

    def vision_controller_path(self, controller):
        request = urllib.request.Request(PUBLIC + '/api/service-authorities/resolve',
            data=json.dumps({'controller': controller}).encode(),
            headers={'Content-Type': 'application/json',
                     'User-Agent': 'BiomeRTE-Rover-Server/1'})
        with urllib.request.urlopen(request, timeout=5) as response:
            scope = json.load(response)
        authority = scope.get('authority')
        if (scope.get('authority_kind') != 'controller' or scope.get('effect_ready') is not True
                or not isinstance(authority, str) or len(authority) != 64
                or any(value not in '0123456789abcdef' for value in authority)
                or controller not in (authority, *scope.get('controller_aliases', ()))):
            raise ConnectionError('Existing vision lease authority is unavailable')
        path = '/api/controller-runtimes/' + authority + '/call'
        if scope.get('call_path') != path:
            raise ConnectionError('Vision authority returned an invalid controller path')
        return path

    def resume_from_bootstrap(self, owner, path, contract, resource, task):
        try:
            prior = read_json(str(self.home / 'vision-stream-resume.json'))
        except (OSError, ValueError):
            return None
        if (not isinstance(prior, dict)
                or (prior.get('contract'), prior.get('resource'), prior.get('task')) != (contract, resource, task)
                or not all(isinstance(prior.get(key), str) and prior[key] for key in ('session', 'lease'))):
            return None
        try:
            descriptor = self.signed_request(owner, 'stream.attach',
                {'session': prior['session'], 'role': 'server', 'lease': prior['lease']}, managed_path=path)
        except HTTPError as error:
            if error.code == 403:
                return None
            if error.code not in (404, 409, 410):
                raise
            # A stale stream hint can refer to an abandoned reservation after
            # the provider briefly disappeared. Only a bound lease can reopen.
            try:
                current = self.signed_request(owner, 'cap.lease',
                    {'lease': prior['lease']}, managed_path=path)
            except HTTPError as lookup_error:
                if lookup_error.code == 404:
                    return None
                raise
            if current.get('state') != 'bound':
                return None
            try:
                descriptor = self.signed_request(owner, 'stream.open',
                    {'lease': prior['lease']}, managed_path=path)
            except HTTPError as reopen_error:
                if reopen_error.code in (404, 409, 410):
                    return None
                raise
        return dict(descriptor, subscription_lease=prior['lease'])

    def resume_vision(self, contract, resource, task):
        owner = self.app.node
        try:
            path = self.vision_controller_path(owner)
            self.vision_scope = (contract, resource, path)
            descriptor = self.resume_from_bootstrap(owner, path, contract, resource, task)
            if descriptor is None:
                descriptor = self.signed_request(owner, 'stream.resume',
                    {'contract': contract, 'resource': resource, 'task': task}, managed_path=path)
        except HTTPError as error:
            if error.code == 404:
                return None
            raise
        lease = descriptor['subscription_lease']
        self.leases[lease] = owner
        self.lease_resources[lease] = (contract, resource)
        self.sessions[descriptor['id']] = owner
        self.session_resources[descriptor['id']] = (contract, resource)
        self.vision_route = (lease, descriptor['id'], path)
        return {'id': lease}, descriptor

    @staticmethod
    def write_grant_reference(path, value):
        try:
            if read_json(str(path)) == value:
                return
        except (OSError, ValueError):
            pass
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.vision-grant-', delete=False) as output:
            temporary = pathlib.Path(output.name)
            try:
                json.dump(value, output)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def remember_vision_grant(self, descriptor, task):
        if descriptor['contract'] != 'robot.vision.stream:1':
            return
        reference = {'session': descriptor['id'], 'lease': descriptor['subscription_lease'],
                     'contract': descriptor['contract'], 'resource': descriptor['resource'],
                     'task': task, 'generation': descriptor['generation']}
        try:
            self.write_grant_reference(self.home / 'vision-stream-resume.json', reference)
        except OSError:
            logging.warning('Could not record the Rover vision subscription reference')
        # A colocated publisher can recover its grant even while its inbox is busy.
        # An independently placed publisher still discovers grants via its inbox.
        root = pathlib.Path(os.environ.get('ROVER_VISION_PROVIDER_STATE',
                                          str(self.home.with_name('biome-robot-vision'))))
        try:
            provider = read_json(str(root / 'provider-manifest.json'))
            if (provider.get('provider'), provider.get('resource')) == (descriptor['provider'], descriptor['resource']):
                fields = ('id', 'epoch', 'authority', 'provider', 'lease', 'generation',
                          'resource', 'contract', 'lanes', 'state', 'expires_at')
                self.write_grant_reference(root / 'provider' / 'vision-stream-resume.json',
                                           {key: descriptor[key] for key in fields})
        except (OSError, ValueError):
            logging.warning('Could not record the colocated vision publisher reference')

    def signed_request(self, controller, operation, arguments, *, managed_path=None):
        if managed_path is None and self.vision_scope is not None:
            lease_id, session_id = arguments.get('lease'), arguments.get('session')
            binding = self.session_resources.get(session_id) or self.lease_resources.get(lease_id)
            if operation == 'cap.acquire':
                binding = (arguments.get('contract'), arguments.get('resource'))
            if binding == self.vision_scope[:2]:
                managed_path = self.vision_scope[2]
        if managed_path is None and self.vision_route is not None:
            lease, session, path = self.vision_route
            if arguments.get('lease') == lease or arguments.get('session') == session:
                managed_path = path
        request_id = uuid.uuid4().hex
        envelope = dict(site=self.app.config['site'], realm='site', operation=operation,
                        arguments=arguments, id=request_id, controller=controller)
        auth = dict(actor=self.identity['node'],
                    signature=sign(str(self.home/'node.key'), envelope))
        managed = managed_path is not None or controller in self.controllers
        lifecycle = operation in ('cap.discover', 'cap.acquire', 'cap.lease',
                                  'stream.open', 'stream.attach', 'stream.resume')
        # A busy managed controller must not make a transient reply failure
        # tear down a persistent reservation. Reuse the exact signed ID.
        delays = iter((.5, 1., 2.5, 4.) if managed and lifecycle else ())
        while True:
            try:
                if managed_path is None and controller == self.app.node:
                    return self.app.call(operation, arguments, request_id, auth=auth,
                                         realm='site', controller=controller)
                envelope['auth'] = auth
                endpoint = PUBLIC + managed_path if managed_path else self.gateway_url + '/v1/call'
                request = urllib.request.Request(endpoint,
                    data=json.dumps(envelope).encode(), headers={
                    'Content-Type':'application/json','User-Agent':'BiomeRTE-Rover-Server/1'})
                with urllib.request.urlopen(request, timeout=20) as response:
                    value = json.load(response)
                if 'error' in value:
                    raise ConnectionError('Controller rejected Rover request')
                return value['result']
            except (HTTPError, TimeoutError) as error:
                if isinstance(error, HTTPError) and error.code not in (429, 502, 503, 504):
                    raise
                delay = next(delays, None)
                if delay is None:
                    raise
                time.sleep(delay)


class FreshRover(Rover):
    def report(self, operation, error):
        if not hasattr(self,'_reported'):self._reported={}
        key=(operation,type(error).__name__,str(error)[:200])
        if time.monotonic()-self._reported.get(key,-100)>30:
            self._reported[key]=time.monotonic()
            logging.warning('Rover %s: %s: %s',operation,type(error).__name__,str(error)[:200])

    async def call(self, operation, **args):
        try:return await super().call(operation,**args)
        except Exception as error:
            self.report(operation,error)
            raise

    async def lane(self, descriptor, lane):
        if getattr(self,'local_router',None):
            descriptor={**descriptor,'url':self.local_router+'/'+descriptor['id']}
        try:return await super().lane(descriptor,lane)
        except Exception as error:
            self.report(lane,error)
            raise

    def broadcast(self, lane, message):
        if lane == 'vision':
            from biomerte.stream_router import decode
            metadata, unused = decode(message)
            if metadata.get('type') in ('sensor_manifest', 'world_snapshot'):
                return super().broadcast(lane, message)
            processed = metadata.get('processed_monotonic')
            # Provider and server share this Mac's monotonic clock. This bound
            # is explicitly processing-to-server, not camera capture latency.
            if type(processed) not in (float,int) or not 0 <= time.monotonic()-processed <= .5:
                return
        super().broadcast(lane, message)


def map_snapshot(rover):
    async def read(map_id, revision):
        if not isinstance(map_id, str) or type(revision) is not int or revision < 0:
            raise web.HTTPBadRequest(text='Expected map ID and revision')
        return await rover.read_capability('robot.map.snapshot:1', {'map_id':map_id, 'revision':revision})
    return read


async def run():
    home=pathlib.Path.home()/'.local/share/biome-rover-server'
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.WARNING,handlers=[RotatingFileHandler(
        home/'runtime.log',maxBytes=1048576,backupCount=1)])
    app=LocalAuthority(pathlib.Path.home()/'.local/share/biomerte-controller',home)
    secret=app.config['stream_router_secret']
    remote=RoutedRemote(app,home)
    clock=await ControlClock().start()
    rover=FreshRover(remote,{'robot.vision.stream:1':'robotvision.mac.ae3c74c06d1909a1ccdc',
                           'device.outputs.stream:1':'relay6ch.30eda0d95f24'},task='rover-site-server',clock=clock)
    rover.local_router=local_router(os.environ.get('BIOMERTE_LOCAL_STREAM_ROUTER'))
    if rover.local_router:
        if not hasattr(rover,'require_provider_ready'):
            raise RuntimeError('Native runtime requires versioned provider-peer readiness support')
        rover.require_provider_ready=True
    bridge=SiteBridge(rover,derived(secret,'rover-site'),
        {'forward':1,'reverse':2,'left':3,'right':4},snapshot=map_snapshot(rover),
        intent_ttl_ms=int(os.environ.get('ROVER_INTENT_TTL_MS','500')))
    application=bridge.app()
    async def inspect_stream(request):
        if not hmac.compare_digest(request.headers.get('X-Router-Inspect',''),derived(secret,'router-inspect')):
            raise web.HTTPUnauthorized()
        value=await request.json()
        if (not isinstance(value,dict) or set(value)!={'authority','session'} or
            value['authority']!=app.node or not isinstance(value['session'],str) or
            len(value['session'])!=32 or any(c not in '0123456789abcdef' for c in value['session'])):
            raise web.HTTPBadRequest()
        try:
            grant=await asyncio.to_thread(app.call,'stream.inspect',{'session':value['session']},uuid.uuid4().hex,realm='site')
            return web.json_response(grant)
        except Exception:
            raise web.HTTPConflict(text='Stream authority unavailable')
    application.router.add_post('/internal/stream/inspect',inspect_stream)
    runner=web.AppRunner(application)
    await runner.setup()
    transport=None
    stopped=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(sig,stopped.set)
    waiter=None
    try:
        await web.TCPSite(runner,'127.0.0.1',8093).start()
        transport=asyncio.create_task(Outbound(PUBLIC,'http://127.0.0.1:8093',secret,rover.local_router).run())
        waiter=asyncio.create_task(stopped.wait())
        done,unused=await asyncio.wait((transport,waiter),return_when=asyncio.FIRST_COMPLETED)
        if transport in done:await transport
    finally:
        if transport:
            transport.cancel()
            with contextlib.suppress(asyncio.CancelledError):await transport
        if waiter:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):await waiter
        await runner.cleanup()
        await clock.close()

if __name__=='__main__':
    try:asyncio.run(run())
    except KeyboardInterrupt:pass
