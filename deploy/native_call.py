"""Forward a native device envelope without granting connector authority."""
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoNativeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def dispatch_native_call(args, local, conf):
    # The destination is connector configuration, never request input.
    url = urlsplit(local)
    if url.scheme not in ('http', 'https') or url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Native relay requires a loopback controller')
    if args.get('site') != conf.get('site'):
        raise ValueError('Native relay site does not match controller')
    wire = args['body']
    e = json.loads(wire)
    if (not isinstance(e, dict) or set(e) != {'site', 'id', 'realm', 'operation', 'arguments', 'auth'}
            or e['site'] != conf.get('site') or e['realm'] != 'site' or not isinstance(e['auth'], dict)
            or set(e['auth']) != {'actor', 'signature'}):
        raise ValueError('Invalid native provider envelope')
    req = Request(local.rstrip('/') + '/v1/call', data=wire.encode('utf-8'),
                  headers={'Content-Type': 'application/json'})
    opener = build_opener(NoNativeRedirect)
    try:
        response = opener.open(req, timeout=30)
    except HTTPError as exc:
        # Native authorization/validation failures are final responses. Server
        # faults stay retryable because their commit outcome can be ambiguous.
        if exc.code >= 500 or exc.code < 400:
            raise
        response = exc
    with response:
        status = response.code
        body = json.loads(response.read().decode('utf-8'))
    if not isinstance(body, dict) or not ('result' in body or 'error' in body):
        raise ValueError('Invalid native controller response')
    value = {'status': status, 'body': body}
    return {'content': [{'type': 'text', 'text': json.dumps(value)}]}
