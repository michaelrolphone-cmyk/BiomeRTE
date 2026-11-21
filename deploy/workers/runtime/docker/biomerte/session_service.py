"""Explicit finite session control and signed packet delivery for local controllers."""
import os
import threading
from urllib.error import HTTPError
from biomerte.store import Fault


class SessionService:
    def __init__(self, home, origin, token, site_access, fetch, deliver=None, app=None):
        self.home, self.origin, self.token = home, origin, token
        self.site_access, self.fetch, self.delivery = site_access, fetch, deliver
        self.directory = os.path.join(home, 'coordination')
        self.app = app
        self._runtime = self._receiver = None
        self._initialization = threading.Lock()

    def runtime(self):
        with self._initialization:
            if self._runtime is None:
                from biomerte.app import Application
                from biomerte.session_runtime import SessionRuntime
                app = self.app or Application(self.home)
                self._runtime = SessionRuntime(app, self.directory, self.remote, app.stores['site'])
        return self._runtime

    def _request(self, path, payload):
        try:
            return self.fetch(self.origin + '/bridge/ha/' + path, payload, self.token,
                              timeout=45, site_access=self.site_access)
        except HTTPError as error:
            raise Fault('Session gateway HTTP ' + str(error.code), error.code)

    def remote(self, operation, payload):
        return self._request('session/' + operation, payload)

    def _blocked(self):
        state = self.runtime().state()
        return state.get('authorization_block') if state else None

    def _record_denial(self, error):
        if isinstance(error, Fault) and error.status == 403:
            runtime = self.runtime()
            # Use the runtime's process-safe lock, not an independent state write.
            from biomerte.session_runtime import serialized
            @serialized
            def block(runtime):
                state = runtime.state()
                if state:
                    state['authorization_block'] = {'status':403, 'reason':str(error)}
                    runtime._set(state)
            block(runtime)

    def deliver(self, recipient, packet):
        if self._blocked():
            raise Fault('Session authorization denied; explicit authorization and resume required', 403)
        from biomerte.session_packets import deliver_packet
        def send(payload):
            response = self._request('packet', payload)
            if 'error' in response:
                raise Fault('Packet recipient rejected delivery', response.get('status', 503))
            return response.get('result', {})
        try:
            return self.delivery(recipient, packet) if self.delivery else deliver_packet(send, recipient, packet)
        except Fault as error:
            self._record_denial(error)
            raise

    def process_packet(self, packet):
        runtime = self.runtime()
        kind = packet.get('payload', {}).get('kind')
        if kind == 'session-invitation':
            state = runtime.join(packet)
            # Never return the invitation's full snapshots/keys as a relay receipt.
            from biomerte.identity import signed
            return signed(runtime.app.key, {'format':1, 'site':runtime.app.site,
                'sender':runtime.app.node, 'to':state['owner'], 'kind':'session-joined',
                'data':{'session':state['id']}})
        if kind == 'session-state':
            return runtime.receive(packet)
        if kind == 'session-acceptance':
            return runtime.settle(packet)
        raise Fault('Unsupported signed coordination packet', 400)

    def accept(self, args):
        runtime = self.runtime()
        with self._initialization:
            if self._receiver is None:
                from biomerte.session_packets import PacketReceiver
                self._receiver = PacketReceiver(os.path.join(self.directory, 'packets'),
                    runtime.app.node, runtime.app.site, self.process_packet)
        return self._receiver.accept(args)

    def invite(self, recipient):
        runtime = self.runtime()
        packet = runtime.invitation(recipient)
        result = self.deliver(recipient, packet)
        body = result.get('payload', {})
        state = runtime.state()
        if (body.get('format') != 1 or body.get('kind') != 'session-joined' or
                body.get('site') != runtime.app.site or body.get('sender') != recipient or
                body.get('to') != runtime.app.node or body.get('data', {}).get('session') != state['id']):
            raise Fault('Invitation acknowledgement mismatch', 403)
        from biomerte.identity import verify
        verify(state['peer_keys'][recipient], result.get('signature'), body)
        return {'joined':recipient, 'session':state['id']}

    def control(self, operation, arguments):
        """Explicit authenticated control; never an unsolicited gateway job."""
        runtime = self.runtime()
        if operation == 'inspect_reconciliation':
            return runtime.inspect_reconciliation()
        if operation == 'reconcile_packet':
            return self.process_packet(arguments['packet'])
        if operation == 'invite':
            return self.invite(arguments['recipient'])
        if operation == 'resume':
            from biomerte.session_runtime import serialized
            @serialized
            def resume(runtime):
                state = runtime.state()
                if not state:
                    raise Fault('No unresolved session', 404)
                state.pop('authorization_block', None)
                runtime._set(state)
                return {'resumed':state['id']}
            return resume(runtime)
        if operation not in ('state', 'open', 'append', 'finalize', 'synchronize', 'recover_open', 'deliver'):
            raise Fault('Unknown session control operation', 400)
        if self._blocked() and operation != 'state':
            raise Fault('Explicit authorization and resume required after denial', 403)
        try:
            if operation == 'deliver':
                return runtime.deliver_pending(self.deliver)
            return getattr(runtime, operation)(**arguments)
        except Fault as error:
            self._record_denial(error)
            raise

    def __call__(self):
        # Idle startup never initializes application databases or creates sessions.
        if not os.path.isfile(os.path.join(self.directory, 'coordination.sqlite')):
            return {'active':False}
        runtime = self.runtime()
        if self._receiver is not None:
            self._receiver.prune()
        elif os.path.isdir(os.path.join(self.directory, 'packets')):
            from biomerte.session_packets import PacketReceiver
            self._receiver = PacketReceiver(os.path.join(self.directory, 'packets'),
                runtime.app.node, runtime.app.site, self.process_packet)
            self._receiver.prune()
        if self._blocked():
            return {'active':False, 'blocked':self._blocked()}
        try:
            result = runtime.maintain()
            state = runtime.state()
            if state and state.get('state') == 'delivery_pending':
                return runtime.deliver_pending(self.deliver)
            return result
        except Fault as error:
            self._record_denial(error)
            raise


def maintenance_callback(home, origin, token, site_access, fetch, deliver=None):
    return SessionService(home, origin, token, site_access, fetch, deliver)


def configured_service(app):
    settings = app.config.get('gateway_link')
    if not isinstance(settings, dict) or not settings.get('origin') or not settings.get('key_file'):
        raise Fault('Explicit local gateway link transport is not configured', 503)
    from .gateway_transport import credentials, fetch
    origin, token, access = credentials(settings['origin'], settings['key_file'])
    return SessionService(app.directory, origin, token, access, fetch, app=app)
