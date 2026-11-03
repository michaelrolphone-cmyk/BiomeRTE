"""MCP Events subscription and delivery state for one trusted controller link."""
import base64
import datetime
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import time
import uuid
from urllib.parse import urlsplit

EVENT = 'workflow.delegation.requested'
DAY = 86400
class CallbackError(Exception):
    pass


def compact(value):
    return json.dumps(value, separators=(',', ':'), sort_keys=True, ensure_ascii=True)


def iso(stamp):
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def destination(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise ValueError('Invalid callback URL')
    p = urlsplit(url)
    if (p.scheme != 'https' or p.username is not None or p.password is not None or
            p.fragment or not p.hostname or p.port not in (None, 443) or
            not re.match(r'^[A-Za-z0-9][A-Za-z0-9.-]+[A-Za-z0-9]$', p.hostname) or
            '.' not in p.hostname or '\\' in url):
        raise ValueError('Expected a public HTTPS callback on port 443')
    return p


# Include special-use ranges older Python 3.4 ipaddress tables did not classify.
SPECIAL = [ipaddress.ip_network(s) for s in (
    '0.0.0.0/8', '10.0.0.0/8', '127.0.0.0/8', '100.64.0.0/10', '169.254.0.0/16',
    '172.16.0.0/12', '192.168.0.0/16', '192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '192.0.0.0/24', '192.88.99.0/24',
    '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4', '2001::/23', '2001:db8::/32',
    '2002::/16', '3fff::/20')]
GLOBAL6 = ipaddress.ip_network('2000::/3')


def public_ip(value):
    addr = ipaddress.ip_address(value)
    return (getattr(addr, 'is_global', not addr.is_private) and not addr.is_multicast and
            (addr.version == 4 or addr in GLOBAL6) and
            not any(addr.version == net.version and addr in net for net in SPECIAL))


def post_callback(url, headers, body):
    p = destination(url)
    addresses = socket.getaddrinfo(p.hostname, 443, 0, socket.SOCK_STREAM)
    if not addresses or any(not public_ip(row[4][0]) for row in addresses):
        raise CallbackError('blocked_address')
    if len(body) > 262144:
        return 413, b''
    deadline = time.monotonic() + 10
    for family, kind, proto, unused, address in addresses:
        if time.monotonic() >= deadline:
            break
        sock = socket.socket(family, kind, proto)
        conn = http.client.HTTPSConnection(p.hostname, timeout=10)
        try:
            sock.settimeout(max(.1, deadline - time.monotonic()))
            sock.connect(address)
            conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=p.hostname)
        except (OSError, ssl.SSLError):
            sock.close()
            conn.close()
            continue
        try:
            conn.sock.settimeout(max(.1, deadline - time.monotonic()))
            conn.request('POST', (p.path or '/') + ('?' + p.query if p.query else ''), body, headers)
            response = conn.getresponse()
            return response.status, response.read(2049)
        finally:
            conn.close()
            sock.close()
    raise CallbackError('callback_unreachable')


def signing_key(value):
    if not isinstance(value, str) or not value.startswith('whsec_'):
        raise ValueError('Invalid signing secret')
    key = base64.b64decode(value[6:].encode('ascii'), validate=True)
    if not 24 <= len(key) <= 64 or base64.b64encode(key).decode('ascii') != value[6:]:
        raise ValueError('Invalid signing secret')
    return key


def signed_post(send, url, sub_id, keys, event_id, event):
    body = compact(event).encode('utf-8')
    ts = str(int(time.time()))
    message = (event_id + '.' + ts + '.').encode('ascii') + body
    sigs = ['v1,' + base64.b64encode(hmac.new(signing_key(k), message, hashlib.sha256).digest()).decode('ascii') for k in keys]
    return send(url, {'Content-Type': 'application/json', 'User-Agent': 'BiomeRTE-Events/1.0',
                      'webhook-id': event_id, 'webhook-timestamp': ts, 'webhook-signature': ' '.join(sigs),
                      'X-MCP-Subscription-Id': sub_id}, body)


class EventStore:
    def __init__(self, path, feed, authorize, post=post_callback, topic_feed=None):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.feed, self.authorize, self.post, self.topic_feed = feed, authorize, post, topic_feed
        self.db.execute('CREATE TABLE IF NOT EXISTS receiver_receipts (id TEXT PRIMARY KEY, signature TEXT, result TEXT)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
          id TEXT PRIMARY KEY, owner TEXT, grant_id TEXT, url TEXT, args TEXT, key TEXT, old_key TEXT,
          rotate_until REAL, verified REAL, expires REAL, cursor INTEGER, failures INTEGER DEFAULT 0,
          next_try REAL DEFAULT 0, generation TEXT, last_error TEXT)''')

    def close(self):
        self.db.close()

    def change(self, method, params, owner, grant):
        if not owner or params.get('name') != EVENT or not isinstance(params.get('arguments'), dict):
            raise ValueError('Unknown event or arguments')
        args = params['arguments']
        if set(args) - {'realm', 'workflow'} or args.get('realm','site') not in ('site','local'):
            raise ValueError('Invalid event filters')
        args = dict(args, realm=args.get('realm','site'))
        if 'workflow' in args and (not isinstance(args['workflow'],str) or
                                    not re.match(r'^[A-Za-z0-9_.:-]{1,128}\Z',args['workflow'])):
            raise ValueError('Invalid workflow filter')
        dest = params.get('delivery') or {}
        if dest.get('mode') != 'webhook':
            raise ValueError('Webhook delivery required')
        url = dest.get('url')
        destination(url)
        sub = 'sub_' + hashlib.sha256(compact([owner, EVENT, args, url]).encode()).hexdigest()
        old = self.db.execute('SELECT * FROM subscriptions WHERE id=?', (sub,)).fetchone()
        if method == 'events/unsubscribe':
            with self.db:
                self.db.execute('DELETE FROM subscriptions WHERE id=?', (sub,))
            return {}
        if method != 'events/subscribe':
            raise ValueError('Unknown event method')
        key = dest.get('secret')
        signing_key(key)
        now = time.time()
        ttl = params.get('ttlMs')
        if ttl is not None and (type(ttl) is not int or ttl < 1):
            raise ValueError('Invalid ttlMs')
        expires = now + min(ttl if ttl is not None else DAY*1000, 7*DAY*1000)/1000.
        value = params.get('cursor')
        if value is not None and (not isinstance(value,str) or not re.match(r'^[0-9]{1,16}\Z',value)):
            raise ValueError('Invalid cursor')
        high = self.feed(args['realm'], 0, 0)['high']
        if value is not None and int(value)>high:
            raise ValueError('Cursor exceeds current history')
        if old and old['expires']>now:
            cursor = old['cursor']
        else:
            cursor = int(value) if value is not None else (old['cursor'] if old else 0)
        cached = old and old['key']==key and old['verified']+300>now
        if not cached:
            challenge = uuid.uuid4().hex + uuid.uuid4().hex
            try:
                code, body = signed_post(self.post, url, sub, [key], 'verify_'+uuid.uuid4().hex,
                                         {'type':'verification','challenge':challenge})
                echoed = json.loads(body.decode('utf-8')).get('challenge') if 200<=code<300 and len(body)<=2048 else None
                if not isinstance(echoed,str) or not hmac.compare_digest(echoed,challenge):
                    raise CallbackError('challenge_failed')
            except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
                raise CallbackError('challenge_failed')
        change_key = old and old['key']!=key
        previous = old['key'] if change_key else (old['old_key'] if old and old['rotate_until']>now else None)
        rotation = now+300 if change_key else (old['rotate_until'] if old else 0)
        with self.db:
            # A delivery may have advanced while verification used the network.
            latest = self.db.execute('SELECT cursor FROM subscriptions WHERE id=?',(sub,)).fetchone()
            if latest and old and old['expires']>now:
                cursor=max(cursor,latest['cursor'])
            self.db.execute('''INSERT OR REPLACE INTO subscriptions
             (id,owner,grant_id,url,args,key,old_key,rotate_until,verified,expires,cursor,generation)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
             (sub,owner,grant,url,compact(args),key,previous,rotation,old['verified'] if cached else now,
              expires,cursor,uuid.uuid4().hex))
        return {'id':sub,'refreshBefore':iso(expires),'cursor':str(cursor),'truncated':False}

    def active(self):
        return self.db.execute('SELECT 1 FROM subscriptions WHERE expires>? LIMIT 1',(time.time(),)).fetchone() is not None

    def advance(self, sub, seq, error=None):
        with self.db:
            self.db.execute('UPDATE subscriptions SET cursor=?,failures=0,next_try=0,last_error=? WHERE id=? AND generation=?',
                            (seq,error,sub['id'],sub['generation']))

    def deliver(self):
        now=time.time()
        rows=self.db.execute('SELECT * FROM subscriptions WHERE expires>? AND next_try<=? ORDER BY next_try,id',
                             (now,now)).fetchall()
        for sub in rows:
            args=json.loads(sub['args'])
            grok = sub['id'].startswith('grok_')
            topic = args.get('topic', EVENT)
            page = (self.feed(args['realm'], sub['cursor'], 16) if topic == EVENT else
                    self.topic_feed(args['realm'], topic, sub['cursor'], 32))
            for item in page['events']:
                seq, data = item['seq'], item['data']
                if topic == EVENT and (item.get('target', 'chatgpt') != ('grok' if grok else 'chatgpt') or
                                       args.get('workflow', data['workflow']) != data['workflow']):
                    self.advance(sub,seq)
                    continue
                if not self.authorize(sub['owner'],sub['grant_id']):
                    with self.db:
                        self.db.execute('DELETE FROM subscriptions WHERE id=? AND generation=?',(sub['id'],sub['generation']))
                    break
                # Re-read after the network check to honor unsubscribe, expiry and rotation.
                if not self.db.execute('SELECT 1 FROM subscriptions WHERE id=? AND generation=? AND expires>?',
                                       (sub['id'],sub['generation'],time.time())).fetchone():
                    break
                eid = 'delegate_' + data['delegation_id'] if topic == EVENT else item['id']
                event = {'eventId':eid, 'name':topic, 'data':data, 'cursor':str(seq)}
                if 'timestamp' in item:
                    event['timestamp'] = item['timestamp']
                if grok:
                    event.update(realm=args['realm'], receiver=args['receiver'])
                keys=[sub['key']]
                if sub['old_key'] and sub['rotate_until']>time.time():
                    keys.append(sub['old_key'])
                try:
                    if grok:
                        from grok_receivers import send_grok
                        status, _ = send_grok(self.post, sub, args, eid, event)
                    else:
                        status, _ = signed_post(self.post,sub['url'],sub['id'],keys,eid,event)
                except (OSError, CallbackError, ValueError, http.client.HTTPException):
                    status=503
                if grok and status in (401, 403, 404, 410):
                    # An invalid key or retired endpoint needs re-registration. Preserve its cursor.
                    with self.db:
                        self.db.execute('UPDATE subscriptions SET next_try=?,last_error=? WHERE id=? AND generation=?',
                                        (sub['expires'], 'receiver needs attention: HTTP '+str(status), sub['id'], sub['generation']))
                    break
                if status==410:
                    with self.db:
                        self.db.execute('DELETE FROM subscriptions WHERE id=? AND generation=?',(sub['id'],sub['generation']))
                    break
                accepted = status == 200 if grok and args['auth_mode'] == 'bearer' else 200 <= status < 300
                if accepted:
                    if grok:
                        with self.db:
                            self.db.execute('UPDATE subscriptions SET verified=? WHERE id=? AND generation=?',
                                            (time.time(),sub['id'],sub['generation']))
                    self.advance(sub,seq)
                    continue
                attempt=sub['failures']+1
                if attempt>=8 or (400<=status<500 and status not in (408,425,429)) or 300<=status<400:
                    # Keep a durable failure receipt and allow later events to proceed.
                    with self.db:
                        self.db.execute('CREATE TABLE IF NOT EXISTS failed_events (sub TEXT, event TEXT, status INTEGER, PRIMARY KEY(sub,event))')
                        self.db.execute('INSERT OR REPLACE INTO failed_events VALUES(?,?,?)',(sub['id'],eid,status))
                    self.advance(sub,seq,'delivery HTTP '+str(status))
                else:
                    with self.db:
                        self.db.execute('UPDATE subscriptions SET failures=?,next_try=?,last_error=? WHERE id=? AND generation=?',
                                        (attempt,time.time()+min(300,2**attempt),str(status),sub['id'],sub['generation']))
                break
            else:
                # Advance over nonmatching controller topics without losing a pending retry.
                scanned = page.get('scanned', sub['cursor'])
                if scanned > sub['cursor'] and (not page['events'] or scanned > page['events'][-1]['seq']):
                    self.advance(sub, scanned)
