"""Private, bounded receipt provenance projection from an original hash chain.

Only a complete genesis-to-independently-observed anchor chain seals evidence.
This module does not fetch sources, pin readers, delete data or modify a Store.
"""
import hashlib
import json
import sqlite3
import time
from .store import Fault, digest

MAX_ENTRY_BYTES = 4194304


class ReceiptProjection:
    def __init__(self, path, anchor_seq, anchor_hash):
        if type(anchor_seq) is not int or anchor_seq < 1 or not isinstance(anchor_hash, str) or len(anchor_hash) != 64:
            raise ValueError('Verified anchor identity required')
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS anchor(id INTEGER PRIMARY KEY,seq INTEGER,hash TEXT,position INTEGER,previous TEXT,sealed INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,first_seq INTEGER,signature TEXT,value_hash TEXT,conflict INTEGER,upper_bound INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS checkpoint_seed(id INTEGER PRIMARY KEY,cursor TEXT,observed_before REAL)')
        row = self.db.execute('SELECT seq,hash FROM anchor WHERE id=1').fetchone()
        if row is None:
            self.db.execute('INSERT INTO anchor VALUES(1,?,?,0,?,0)',(anchor_seq,anchor_hash,''))
        elif row != (anchor_seq,anchor_hash):
            raise Fault('Receipt provenance anchor changed',409)
        self.db.commit()

    def append(self, encoded):
        """One <=4MiB original entry per transaction; ordered, no gaps/retries."""
        if self.db.execute('SELECT 1 FROM checkpoint_seed').fetchone():
            raise Fault('Checkpoint cohort cannot accept genesis replay',409)
        raw = encoded.encode('utf-8')
        if len(raw) > MAX_ENTRY_BYTES:
            raise Fault('Receipt provenance entry exceeds bound',413)
        end, end_hash, position, previous, sealed = self.db.execute(
            'SELECT seq,hash,position,previous,sealed FROM anchor WHERE id=1').fetchone()
        entry = json.loads(encoded)
        if sealed or entry.get('seq') != position+1 or type(entry.get('seq')) is not int or entry.get('prev') != previous or entry['seq']>end:
            raise Fault('Receipt provenance chain discontinuity',409)
        if not isinstance(entry.get('changes'),list):
            raise Fault('Invalid provenance changes',503)
        hashed = hashlib.sha256(raw).hexdigest()
        if entry['seq']==end and hashed != end_hash:
            raise Fault('Receipt provenance final anchor mismatch',503)
        try:
            for change in entry['changes']:
                if not isinstance(change,list) or len(change)!=3:
                    raise Fault('Invalid provenance change',503)
                kind,key,value = change
                if kind != '_receipt':
                    continue
                if not isinstance(key,str):
                    raise Fault('Invalid receipt identity',503)
                old = self.db.execute('SELECT first_seq,signature,conflict FROM receipts WHERE id=?',(key,)).fetchone()
                if not isinstance(value,dict) or not isinstance(value.get('signature'),str):
                    if old:
                        self.db.execute('UPDATE receipts SET conflict=1 WHERE id=?',(key,))
                    continue
                signature = value['signature']
                conflict = int(bool(old and (old[2] or old[1]!=signature)))
                self.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?,0)',
                                (key,old[0] if old else entry['seq'],signature,digest(value),conflict))
            self.db.execute('UPDATE anchor SET position=?,previous=?,sealed=? WHERE id=1',
                            (entry['seq'],hashed,int(entry['seq']==end)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {'seq':entry['seq'],'sealed':entry['seq']==end}

    def seed_checkpoint(self, stage, batch=64):
        """Page a successful GatewayJournal CheckpointStage, never a raw DB.

        Its authenticated manifest and every value were verified by the native
        reader. Caller holds/renews its own stage pin until ingestion completes.
        This is an existence upper bound, not an asserted first-write position.
        """
        from .gateway_journal import CheckpointStage
        if not isinstance(stage, CheckpointStage) or type(batch) is not int or not 1<=batch<=64:
            raise ValueError('Verified checkpoint stage and bounded batch required')
        end, hashed, position, sealed = self.db.execute('SELECT seq,hash,position,sealed FROM anchor WHERE id=1').fetchone()
        if (stage['seq'],stage['hash'])!=(end,hashed) or sealed or position:
            raise Fault('Checkpoint evidence identity/state mismatch',409)
        row=self.db.execute('SELECT cursor FROM checkpoint_seed WHERE id=1').fetchone()
        cursor=row[0] if row else ''
        rows=stage.db.execute("SELECT id,length(CAST(value AS BLOB)) FROM records WHERE kind='_receipt' AND id>? ORDER BY id LIMIT ?",(cursor,batch)).fetchall()
        used=0;count=0
        try:
            self.db.execute('INSERT OR IGNORE INTO checkpoint_seed VALUES(1,?,?)',('',time.time()))
            for key,size in rows:
                if size>MAX_ENTRY_BYTES:
                    raise Fault('Receipt checkpoint evidence exceeds bound',413)
                if used+size>MAX_ENTRY_BYTES:break
                value=json.loads(stage.db.execute("SELECT value FROM records WHERE kind='_receipt' AND id=?",(key,)).fetchone()[0])
                used+=size;count+=1;cursor=key
                if isinstance(value,dict) and isinstance(value.get('signature'),str):
                    self.db.execute('INSERT INTO receipts VALUES(?,?,?,?,0,1)',
                                    (key,end,value['signature'],digest(value)))
            done=len(rows)<batch and count==len(rows)
            self.db.execute('UPDATE checkpoint_seed SET cursor=? WHERE id=1',(cursor,))
            if done:
                self.db.execute('UPDATE anchor SET position=?,previous=?,sealed=1 WHERE id=1',(end,hashed))
            self.db.commit()
        except BaseException:
            self.db.rollback();raise
        return {'records':count,'bytes':used,'sealed':done}

    def status(self):
        seq,hashed,position,sealed = self.db.execute('SELECT seq,hash,position,sealed FROM anchor WHERE id=1').fetchone()
        return {'anchor_seq':seq,'anchor_hash':hashed,'position':position,'sealed':bool(sealed)}

    def close(self):
        self.db.close()


def plan_receipts(tx, projection, after='', batch=64):
    """Only sealed evidence; exact current values, <=64 rows/1MiB per page."""
    status=projection.status()
    if not status['sealed']:
        raise Fault('Unsealed receipt provenance cannot enroll records',409)
    if type(batch) is not int or not 1<=batch<=64:
        raise ValueError('Receipt evidence batch must be 1..64')
    rows=projection.db.execute('SELECT id,first_seq,signature,value_hash,conflict,upper_bound FROM receipts WHERE id>? ORDER BY id LIMIT ?',(after,batch)).fetchall()
    result={'anchor':status,'after':after,'batch':batch,'next':after,'complete':len(rows)<batch,
            'markers':[],'preimages':[],'exceptions':[],'bytes':0}
    for key,first,signature,expected,conflict,upper_bound in rows:
        result['next']=key
        size=tx.db.execute("SELECT length(CAST(value AS BLOB)) FROM records WHERE kind='_receipt' AND id=?",(key,)).fetchone()
        if size and (size[0]>1048576 or result['bytes']+size[0]>1048576):
            result['exceptions'].append([key,'payload_budget']);continue
        value=tx.get('_receipt',key)
        result['bytes']+=size[0] if size else 0
        result['preimages'].append([key,digest(value)])
        if conflict or value is None or digest(value)!=expected:
            result['exceptions'].append([key,'receipt_changed_missing_or_conflicting']);continue
        if value.get('result_expired'):
            result['exceptions'].append([key,'already_retired']);continue
        if 'result' not in value or value.get('signature')!=signature or len(key)+9>512:
            result['exceptions'].append([key,'incomplete_receipt_identity']);continue
        marker_key='_receipt:'+key
        marker=tx.get('_retention',marker_key)
        result['preimages'].append([marker_key,digest(marker)])
        updated=dict(marker or {'id':marker_key,'kind':'_receipt','key':key,'historical':True})
        updated['completed_order']=first
        updated['order_source']='journal'
        updated['order_upper_bound']=bool(upper_bound)
        if upper_bound:
            observed=projection.db.execute('SELECT observed_before FROM checkpoint_seed WHERE id=1').fetchone()[0]
            updated['observed_before']=min(observed,updated.get('observed_before',observed))
        if updated!=marker:result['markers'].append(updated)
    return result


def apply_receipts(tx, projection, approved):
    current=plan_receipts(tx,projection,approved['after'],approved['batch'])
    if digest(current)!=digest(approved):
        raise Fault('Receipt provenance dry-run preimage changed',409)
    for marker in current['markers']:
        tx.put('_retention',marker['id'],marker)
    return {'enrolled':len(current['markers']),'next':current['next'],'complete':current['complete'],
            'exceptions':current['exceptions']}


def observe_live_receipts(store, path, after='', batch=64, maximum_tail=64):
    """Seal one bounded current-state cohort using a fresh authenticated head.

    Each page has its own exact anchor and observation bound. No claim of first
    creation is made. A private Store copy disables checkpoint fetch and refuses
    more than maximum_tail journal entries, so this cannot start a full restore.
    The ordinary Store hash-chain validation remains unchanged.
    """
    import copy
    import os
    from .store import Store
    from .gateway_journal import GatewayJournal
    if (not isinstance(store, Store) or not isinstance(store.remote, GatewayJournal) or
            type(batch) is not int or not 1 <= batch <= 64 or
            type(maximum_tail) is not int or not 0 <= maximum_tail <= 64):
        raise ValueError('Existing authenticated Store and bounded page required')
    db=store.connect()
    try:baseline=db.execute('SELECT seq FROM position WHERE id=1').fetchone()[0]
    finally:db.close()
    class BoundedRead:
        def __init__(self):self.calls=0;self.head=None
        def read(self,key):
            self.calls+=1
            if self.calls>maximum_tail+2:raise Fault('Live cohort transport budget exceeded',409)
            if key!='head':
                if self.head is None or not key.startswith('log/'):
                    raise Fault('Unexpected live cohort read',409)
                number=int(key[4:])
                if not baseline<number<=self.head['seq']:
                    raise Fault('Live cohort tail outside bound',409)
            value,version=store.remote.read(key)
            if key=='head':
                if (not isinstance(value,dict) or type(value.get('seq')) is not int or
                        not baseline<=value['seq']<=baseline+maximum_tail or
                        not isinstance(value.get('hash'),str) or len(value['hash'])!=64):
                    raise Fault('Live cohort requires an already caught-up replica',409)
                self.head=value
            return value,version
    guarded=copy.copy(store);guarded.remote=BoundedRead()
    def observe(tx):
        position=tx.db.execute('SELECT seq,hash FROM position WHERE id=1').fetchone()
        if position!=(guarded.remote.head['seq'],guarded.remote.head['hash']):
            raise Fault('Live cohort verified position differs',409)
        rows=tx.db.execute("SELECT id,length(CAST(value AS BLOB)) FROM records WHERE kind='_receipt' AND id>? ORDER BY id LIMIT ?",(after,batch)).fetchall()
        items=[];exceptions=[];used=0;cursor=after
        for key,size in rows:
            cursor=key
            if size>1048576 or used+size>1048576:
                exceptions.append([key,'payload_budget']);continue
            value=tx.get('_receipt',key);used+=size
            if not isinstance(value,dict) or not isinstance(value.get('signature'),str):
                exceptions.append([key,'incomplete_receipt_identity']);continue
            items.append((key,value['signature'],digest(value)))
        return {'anchor_seq':position[0],'anchor_hash':position[1],'observed_before':time.time(),
                'items':items,'exceptions':exceptions,'after':after,'next':cursor,
                'complete':len(rows)<batch,'bytes':used}
    evidence=guarded.execute(observe,cached=False,read_only=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,384);os.close(fd)
    projection=ReceiptProjection(path,evidence['anchor_seq'],evidence['anchor_hash'])
    try:
        with projection.db:
            projection.db.execute('INSERT INTO checkpoint_seed VALUES(1,?,?)',(evidence['next'],evidence['observed_before']))
            for key,signature,hashed in evidence['items']:
                projection.db.execute('INSERT INTO receipts VALUES(?,?,?,?,0,1)',(key,evidence['anchor_seq'],signature,hashed))
            projection.db.execute('UPDATE anchor SET position=seq,previous=hash,sealed=1 WHERE id=1')
    finally:projection.close()
    return {key:value for key,value in evidence.items() if key!='items'}
