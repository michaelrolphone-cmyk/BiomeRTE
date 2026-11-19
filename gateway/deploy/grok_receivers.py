"""Grok webhook receiver configuration and wire formats, shared by both realms."""
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from mcp_events import EVENT, LEASE_EVENT, compact, destination, iso, signed_post, signing_key


def identifier(value):
    if not isinstance(value, str) or not re.match(r'^[A-Za-z0-9_.:-]{1,128}\Z', value):
        raise ValueError('Expected an identifier of 1–128 letters, digits, dots, colons, underscores or dashes.')
    return value


def public_record(row):
    args = json.loads(row['args'])
    result = {k: args[k] for k in ('receiver', 'realm', 'topic', 'auth_mode')}
    for field in ('workflow', 'reply_to'):
        if field in args:
            result[field] = args[field]
    result.update(origin='https://' + urlsplit(row['url']).hostname, cursor=row['cursor'],
                  last_accepted_at=iso(row['verified']) if row['verified'] else None,
                  last_error=row['last_error'])
    return result


def mutate(store, action, args, owner, grant):
    if not owner or not isinstance(args, dict):
        raise ValueError('An authorized owner and arguments are required.')
    db = store.db
    if action == 'list':
        if args:
            raise ValueError('The list tool takes no arguments.')
        rows = db.execute("SELECT * FROM subscriptions WHERE id GLOB 'grok_*' AND owner=? AND (? IS NULL OR grant_id=?) ORDER BY id",
                          (owner, grant, grant))
        return {'receivers': [public_record(row) for row in rows]}
    if action not in ('register', 'unregister'):
        raise ValueError('Unknown receiver operation.')
    receiver = identifier(args.get('receiver'))
    rid = identifier(args.get('request_id'))
    realm = args.get('realm', 'site')
    if realm not in ('site', 'local'):
        raise ValueError('Unknown realm.')
    base = {'receiver','request_id','realm'}
    permitted = base | {'topic','workflow','reply_to','url','key','auth_mode','from_cursor'} if action == 'register' else base
    if set(args) - permitted:
        raise ValueError('Unsupported receiver argument.')
    sub_id = 'grok_' + hashlib.sha256(compact([owner, realm, receiver]).encode()).hexdigest()
    receipt = hashlib.sha256(compact([owner, grant, rid]).encode()).hexdigest()
    signature = hashlib.sha256(compact([action, args]).encode()).hexdigest()
    prior = db.execute('SELECT * FROM receiver_receipts WHERE id=?', (receipt,)).fetchone()
    if prior:
        if prior['signature'] != signature:
            raise ValueError('request_id already used with other arguments.')
        return json.loads(prior['result'])
    old = db.execute('SELECT * FROM subscriptions WHERE id=?', (sub_id,)).fetchone()
    if old and grant is not None and old['grant_id'] != grant:
        raise ValueError('Receiver belongs to another connection.')
    with db:
        if action == 'unregister':
            db.execute('DELETE FROM subscriptions WHERE id=?', (sub_id,))
            result = {'receiver':receiver, 'realm':realm, 'removed':bool(old)}
        else:
            topic = identifier(args.get('topic', EVENT))
            config = {'receiver':receiver, 'realm':realm, 'topic':topic,
                      'auth_mode':args.get('auth_mode','bearer')}
            if 'workflow' in args:
                if topic != EVENT:
                    raise ValueError('workflow is a filter on workflow.delegation.requested.')
                config['workflow'] = identifier(args['workflow'])
            if 'reply_to' in args:
                if topic != LEASE_EVENT or not isinstance(args['reply_to'], str) or not 1 <= len(args['reply_to']) <= 160:
                    raise ValueError('reply_to is an inbox filter on lease.available.')
                config['reply_to'] = args['reply_to']
            url, key = args.get('url'), args.get('key')
            dest = destination(url)
            if dest.path in ('', '/'):
                raise ValueError('Supply the routine-specific webhook URL.')
            if config['auth_mode'] == 'standard_webhooks':
                signing_key(key)
            elif config['auth_mode'] != 'bearer' or not isinstance(key,str) or not re.match(r'^[\x21-\x7e]{1,4096}\Z',key):
                raise ValueError('Supply a raw bearer key or a Standard Webhooks signing secret.')
            old_args = json.loads(old['args']) if old else {}
            same_feed = old and old_args['topic']==topic and all(old_args.get(k)==config.get(k) for k in ('workflow','reply_to'))
            if 'from_cursor' in args or not same_feed:
                feed = store.feed(realm,0,0) if topic==EVENT else store.topic_feed(realm,topic,0,0)
                cursor = args.get('from_cursor',feed['high'])
                if type(cursor) is not int or not 0 <= cursor <= feed['high']:
                    raise ValueError('from_cursor is outside current history.')
            else:
                cursor = old['cursor']
            # Acquiring the write lock before re-reading avoids rolling back concurrent delivery progress.
            db.execute('BEGIN IMMEDIATE')
            if same_feed and 'from_cursor' not in args:
                current = db.execute('SELECT cursor FROM subscriptions WHERE id=?',(sub_id,)).fetchone()
                if current:
                    cursor = current['cursor']
            accepted = old['verified'] if old and old['url']==url and old['key']==key else 0
            db.execute('''INSERT OR REPLACE INTO subscriptions
                       (id,owner,grant_id,url,args,key,rotate_until,verified,expires,cursor,generation)
                       VALUES(?,?,?,?,?,?,0,?,253402300799,?,?)''',
                       (sub_id, owner, grant, url, compact(config), key, accepted, cursor, uuid.uuid4().hex))
            result = public_record(db.execute('SELECT * FROM subscriptions WHERE id=?',(sub_id,)).fetchone())
        db.execute('INSERT INTO receiver_receipts VALUES(?,?,?)',(receipt,signature,compact(result)))
    return result


def send_grok(send, row, config, event_id, event):
    if config['auth_mode'] == 'standard_webhooks':
        return signed_post(send,row['url'],row['id'],[row['key']],event_id,event)
    headers = {'Content-Type':'application/json','User-Agent':'BiomeRTE-Events/1.0',
               'Authorization':'Bearer '+row['key'],'Idempotency-Key':event_id}
    return send(row['url'],headers,compact(event).encode('utf-8'))
