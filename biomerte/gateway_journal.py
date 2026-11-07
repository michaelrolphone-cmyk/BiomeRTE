"""Shared, compare-and-swap journal through the authenticated MCP gateway."""
import json
import ssl
from urllib.request import Request, build_opener, HTTPSHandler
from urllib.error import HTTPError
from .store import Fault, encode


class JournalConflict(Fault):
    pass


class GatewayJournal:
    def __init__(self, settings, site):
        self.origin = settings['origin'].rstrip('/')
        if not self.origin.startswith('https://'):
            raise Fault('Gateway journal requires HTTPS')
        self.site = site
        self.key_file = settings['key_file']
        self.timeout = settings.get('network_timeout', 45)

    def request(self, path, payload):
        with open(self.key_file) as handle:
            token = handle.read().strip()
        request = Request(self.origin + '/bridge/ha/' + path,
                          encode(payload).encode('utf-8'),
                          {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                           'User-Agent': 'BiomeRTE-Controller-Link/1.0'})
        try:
            opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))
            with opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            if exc.code == 409 and path == 'append':
                raise JournalConflict('Shared head advanced. Retry the same request ID.', 409)
            raise Fault('Gateway journal HTTP ' + str(exc.code), 503)
        except (OSError, ValueError):
            raise Fault('Gateway journal unavailable', 503)

    def read(self, name):
        result = self.request('read', {'key': name})
        value = json.loads(result['encoded']) if result.get('encoded') is not None else result.get('value')
        return value, result.get('version', '0')

    def append(self, expected, entry):
        return self.request('append', {'expected': int(expected), 'entry': encode(entry)})

    def status(self):
        return self.request('status', {})
