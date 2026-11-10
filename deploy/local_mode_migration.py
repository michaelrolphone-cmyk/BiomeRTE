"""Offline, fail-closed preparation of independent controller site.sqlite files.

This tool NEVER changes node.json, private keys, local.sqlite, worker ledgers,
services, gateway state or the source mirror. ``stage`` writes a NEW candidate
file only, outside the controller home. It is not a cutover command.

Run inventory first, using the configured gateway/etcd mirror (not old
site.sqlite). Supply a reviewed manifest bound to that inventory SHA256:
  format: 1, site: <site>, inventory_sha256: <inventory digest>
  controllers: {application_node: {transport: <registered ID>, evidence: <ref>}}
  providers: {provider_id: {owner: <application_node>, evidence: <ref>,
              authentication: {mode: native_peer|gateway_oauth, realm: site,
                               evidence: <verified existing path>,
                               actor: <explicit native peer, when signed>,
                               endpoint: <existing HTTPS MCP URL, when OAuth>}}}
  resources: {resource: {owner: <application_node>, provider: <ID>, evidence: <ref>}}
  records: {kind: {id: <application_node or "shared">}}
  qualifications: {<each name in QUALIFICATIONS>: <verified evidence reference>}
Every current record needs an explicit disposition; only inert/reference kinds
may be shared. Physical resources/providers are never inferred from names or
fingerprints. Dependencies are checked conservatively, including references in
extension records. Cross-owner or unknown dependencies require reconciliation,
not silent omission. Dynamic active process routes are a qualification blocker.
Work, grants, subscriptions, events and mission allocation always have one owner.
Pending event consumers must stay with their events; allocation spanning resource
owners requires a separately qualified per-owner policy. The helper does not
reconstruct event delivery stores or turn historic event payloads into snapshots.

Both controllers must use the SAME manifest and source inventory. Actual native
inventory, stopped writers, exact mirror head, provider polling destinations,
fence/worker-ledger continuity and event-delivery placement must be independently
verified. Evidence strings are references, not an automated verification of those
facts. Controller peer trust is NOT granted here. Existing signed offline/result
exchange and finite coordination sessions remain separate protocols.

No journal/log/checkpoint or disposable index is copied. Only current canonical
records are selected, with existing retention eligibility intact; foreign offers
are offline and all execution/business state has exactly one owner. The new
position is zero. Original identities/ledgers stay in place, never regenerated.
Source cleanup and atomic host activation require a separate qualified procedure.
Payloads stream one at a time. Defaults cap current-state input at 50,000 rows,
32 MiB total and 1 MiB per row, plus 200,000 dependency checks. The candidate has
a matching SQLite page/byte cap. Limits are implementation defaults, not a user
retention policy; exceeding them aborts, never truncates or starts a backup.

Commands: inventory --home HOME; retention --home HOME --at UNIX_SECONDS;
plan --home HOME --manifest MANIFEST;
stage --home HOME --manifest MANIFEST --output PRIVATE_DIR/site.sqlite
      --plan-sha256 REVIEWED_PLAN_SHA256.
PRIVATE_DIR must already exist, be canonical and owner-only (mode 0700).
"""
import argparse
import bisect
import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import sys
from urllib.parse import quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from biomerte.store import encode

QUALIFICATIONS = (
    'writers_stopped', 'mirror_head_verified', 'provider_routes_verified',
    'provider_ledgers_preserved', 'event_delivery_placement_verified',
    'controller_routes_verified', 'caller_routes_verified', 'native_inventory_reviewed',
)
# These records cannot start processes or consume a resource by themselves.
SHARED_KINDS = frozenset((
    'contracts', '_contract_usage', '_retired_contracts', '_builtin_contracts',
    'definitions', 'process_versions', 'recipes', 'documents', 'revisions',
    '_writers', 'artifacts',
))
SHARED_META = frozenset(('initialized', 'events', 'delegation_sequence',
                         'operational_retention'))
# Unknown extensions are not assumed inert. Add schema/dependency tests first.
OWNED_KINDS = frozenset((
    'offers', '_offer_usage', '_capacity', '_enforcers', 'guards', 'leases',
    'actions', 'activations', '_lease_queue', '_reservation_activity',
    '_submissions', '_submission_active', '_submission_leases',
    'work', 'runs', 'waits', 'subscriptions', 'messages', 'demands', 'events', 'mission',
    'packages', 'grants', 'incoming', 'peers', 'candidates', 'delegation_events',
    '_receipt', '_receipt_stream', '_retention', '_meta', 'recovery_compactions',
    'decisions', '_decision_devices', '_decision_pairings', '_decision_responses',
    'authorization_grants', 'authorization_requests', 'authorization_usage',
    '_authorization_protected', '_authorization_resolutions',
    'assistant_acceptances', 'assistant_assignment', 'assistant_background',
    'assistant_candidates', 'assistant_checkpoints', 'assistant_config',
    'assistant_control_receipts', 'assistant_cycle_inputs', 'assistant_dispatch_failures',
    'assistant_episode_intents', 'assistant_episodes', 'assistant_finalizations',
    'assistant_frames', 'assistant_incidents', 'assistant_inputs',
    'assistant_learning_outbox', 'assistant_materialized', 'assistant_rejections',
    'assistant_responsibilities', 'assistant_reviews', 'assistant_start_outbox',
    'assistant_supervision_receipts', 'assistant_trigger_receipts', 'assistant_wake_outbox',
    'impact_index', 'impact_queue',
))
VIRTUAL = frozenset(('builtin', 'routine'))
DEFAULT_LIMITS = {'rows': 50000, 'bytes': 32 * 1024 * 1024, 'row_bytes': 1024 * 1024}
DEPENDENCY_LIMIT = 200000


class QualificationError(ValueError):
    pass


def sha(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def load_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def source_path(home, config):
    if config.get('gateway_journal') and config.get('etcd'):
        raise QualificationError('Mixed shared storage configuration')
    if config.get('gateway_link'):
        raise QualificationError('Already configured for local mode; do not re-import an old mirror')
    site = config.get('site')
    if not isinstance(site, str) or not site or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in site):
        raise QualificationError('Invalid site identifier')
    prefix = 'gateway-' if config.get('gateway_journal') else 'cluster-' if config.get('etcd') else None
    if prefix is None:
        raise QualificationError('No shared mirror configured; local data needs no shared-store migration')
    return os.path.join(home, prefix + site + '.sqlite')


class RecordSnapshot:
    """Read one bounded payload at a time from the open read-only transaction."""
    def __init__(self, db, count):
        self.db, self.count = db, count

    def __len__(self):
        return self.count

    def __iter__(self):
        return iter(self.db.execute('SELECT kind,id FROM records ORDER BY kind,id'))

    def __contains__(self, ref):
        return self.db.execute('SELECT 1 FROM records WHERE kind=? AND id=?', ref).fetchone() is not None

    def get(self, ref, default=None):
        row = self.db.execute('SELECT value FROM records WHERE kind=? AND id=?', ref).fetchone()
        return json.loads(row[0]) if row else default

    def __getitem__(self, ref):
        row = self.db.execute('SELECT value FROM records WHERE kind=? AND id=?', ref).fetchone()
        if row is None:
            raise KeyError(ref)
        return json.loads(row[0])

    def items(self):
        for ref in self:
            yield ref, self[ref]

    def kind_items(self, kind):
        for key, raw in self.db.execute('SELECT id,value FROM records WHERE kind=? ORDER BY id', (kind,)):
            yield (kind, key), json.loads(raw)


class CandidateSnapshot:
    """Streaming retained view; only small policy metadata is overlaid."""
    def __init__(self, source, omitted, overlay, patches=None):
        self.source, self.omitted, self.overlay = source, omitted, overlay
        self.patches = patches or {}

    def __iter__(self):
        return iter(sorted((set(self.source) - self.omitted) | set(self.overlay)))

    def __contains__(self, ref):
        return ref in self.overlay or ref not in self.omitted and ref in self.source

    def get(self, ref, default=None):
        if ref in self.overlay:
            return self.overlay[ref]
        value = default if ref in self.omitted else self.source.get(ref, default)
        if ref in self.patches:
            value = dict(value)
            for field in self.patches[ref]:
                value.pop(field)
        return value

    def __getitem__(self, ref):
        if ref not in self:
            raise KeyError(ref)
        return self.get(ref)

    def items(self):
        for ref in self:
            yield ref, self[ref]

    def kind_items(self, kind):
        for ref in self:
            if ref[0] == kind:
                yield ref, self[ref]


def retained_snapshot(records, manifest):
    """Recompute a reviewed retention manifest, never trust supplied omissions."""
    selected = manifest.get('retention_selection')
    if selected is None:
        return records, None
    if not isinstance(selected, dict) or not isinstance(selected.get('policy'), dict):
        raise QualificationError('Invalid retention selection')
    from biomerte.retention_selection import plan as select
    policy = selected['policy']
    actual = select(records, policy.get('now'), seconds=policy.get('seconds'),
                    keep=policy.get('keep'), historical=policy.get('historical'),
                    dependency_limit=manifest.get('dependency_limit', DEPENDENCY_LIMIT),
                    repair_review=selected.get('repair_review'))
    if sha(actual) != sha(selected):
        raise QualificationError('Retention selection changed or unverified omissions supplied')
    omitted = {(item['kind'], item['id']) for item in actual['omitted']}
    overlay = {('_retention', item['id']): item['value'] for item in actual['marker_updates']}
    if actual['event_floor']['after'] != actual['event_floor']['before']:
        meta = dict(records.get(('_meta', 'operational_retention'), {}))
        meta['event_floor'] = actual['event_floor']['after']
        overlay[('_meta', 'operational_retention')] = meta
    patches = {(item['kind'], item['id']): item['remove_fields'] for item in actual.get('record_updates', [])}
    return CandidateSnapshot(records, omitted, overlay, patches), actual


@contextlib.contextmanager
def snapshot(home, limits=None):
    """One read-only SQLite snapshot, including committed WAL, with hard bounds."""
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    if any(type(v) is not int or v < 1 for v in limits.values()):
        raise QualificationError('Invalid inventory bounds')
    config = load_json(os.path.join(home, 'node.json'))
    path = source_path(home, config)
    if not os.path.isfile(path) or os.path.islink(path):
        raise QualificationError('Configured mirror must be an existing regular file')
    db = sqlite3.connect('file:' + quote(os.path.abspath(path), safe='/') + '?mode=ro', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        position = db.execute('SELECT seq,hash FROM position WHERE id=1').fetchone()
        if not position or type(position[0]) is not int or not isinstance(position[1], str):
            raise QualificationError('Invalid source journal position')
        count, total, h = 0, 0, hashlib.sha256()
        for kind, key, size in db.execute('SELECT kind,id,length(CAST(value AS BLOB)) FROM records ORDER BY kind,id'):
            if not isinstance(kind, str) or not isinstance(key, str) or type(size) is not int:
                raise QualificationError('Invalid canonical record')
            size += len(kind.encode('utf-8')) + len(key.encode('utf-8'))
            if count >= limits['rows'] or size > limits['row_bytes'] or total + size > limits['bytes']:
                raise QualificationError('Inventory exceeds configured bounds; no partial migration is allowed')
            raw = db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, key)).fetchone()[0]
            value = json.loads(raw)
            canonical = encode([kind, key, value]).encode('utf-8')
            h.update(canonical + b'\n')
            count += 1
            total += size
        description = {'format': 1, 'site': config['site'], 'position': list(position),
                       'records_sha256': h.hexdigest(), 'rows': count}
        yield config, path, RecordSnapshot(db, count), dict(description, sha256=sha(description), bytes=total)
    finally:
        db.close()


def inventory(home, limits=None):
    """Bounded deterministic metadata, never values, credentials or payloads."""
    with snapshot(home, limits) as (config, path, records, summary):
        counts, rows = {}, []
        for (kind, key), value in records.items():
            counts[kind] = counts.get(kind, 0) + 1
            item = {'kind': kind, 'id': key, 'sha256': sha(value)}
            if isinstance(value, dict):
                for field in ('state', 'provider', 'resource', 'lease', 'contract'):
                    if isinstance(value.get(field), str):
                        item[field] = value[field]
            rows.append(item)
        return dict(summary, node=config['node'], source=os.path.basename(path), counts=counts, records=rows)


def strings(value):
    if isinstance(value, str):
        yield value
        if value.startswith('action:'):
            yield value[7:]
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def retirement_inventory(home, site, max_entries=1024):
    """Describe exact local retirement candidates, without traversing or deleting."""
    names = os.listdir(home)
    if len(names) > max_entries:
        raise QualificationError('Controller directory exceeds retirement inventory bound')
    mirrors = {prefix + site + '.sqlite' + suffix
               for prefix in ('gateway-', 'cluster-') for suffix in ('', '-wal', '-shm', '-journal')}
    items = []
    for name in sorted(names):
        cache = name.startswith(('biomerte-checkpoint-', 'biomerte-replay-'))
        if name not in mirrors and not cache:
            continue
        info = os.lstat(os.path.join(home, name))
        items.append({'path': name, 'kind': 'checkpoint_or_replay_cache' if cache else 'legacy_mirror',
                      'size_bytes': info.st_size if stat.S_ISREG(info.st_mode) else None,
                      'symlink': stat.S_ISLNK(info.st_mode),
                      'qualification': 'verify exact closed artifact and contents before retirement'})
    return {'items': items, 'deletion_performed': False, 'recovery_copy_created': False,
            'existing_site_requires_reconciliation': os.path.lexists(os.path.join(home, 'site.sqlite')),
            'remote_retirement_unqualified': ['old shared journal', 'checkpoint objects', 'shared event snapshots'],
            'completion_gate': 'All owners adopted and verified; providers/callers pinned; unresolved operations, fences, business work and event delivery accounted for; legacy writers stopped.',
            'disposal_lifecycle': 'After the completion gate, verify and remove exact retired artifacts under separate authorization. Any explicitly approved temporary recovery copy needs a recorded expiry deadline and deletion verification; no permanent snapshot is created by this helper.'}


def inert_mission(key, value):
    """Only the existing allocation-policy schema, never arbitrary work state."""
    if key != 'current' or not isinstance(value, dict) or set(value) - {
            'budget', 'remaining', 'services', 'recovery_samples', 'allocation', 'unmet'}:
        return False
    allocation = value.get('allocation')
    if not isinstance(allocation, dict) or any(not isinstance(k, str) or v is not None and not isinstance(v, str)
                                               for k, v in allocation.items()):
        return False
    services = value.get('services', [])
    if not isinstance(services, list):
        return False
    for service in services:
        if not isinstance(service, dict) or set(service) - {'id', 'priority', 'modes', 'needs'}:
            return False
        if not isinstance(service.get('modes', []), list):
            return False
        if any(not isinstance(mode, dict) or set(mode) - {'name', 'cost'} for mode in service.get('modes', [])):
            return False
    return True


def _plan(config, path, records, summary, manifest, retention_selection=None):
    errors, owners, changes = set(), {}, []
    qualified_repairs = {(r['kind'], r['id']) for r in (retention_selection or {}).get('qualified_repairs', [])}
    controllers = manifest.get('controllers', {})
    providers, resources = manifest.get('providers', {}), manifest.get('resources', {})
    if manifest.get('format') != 1 or manifest.get('site') != config['site']:
        errors.add('Manifest format/site mismatch')
    if manifest.get('inventory_sha256') != summary['sha256']:
        errors.add('Manifest is not bound to this exact source inventory and position')
    if not isinstance(controllers, dict) or config['node'] not in controllers:
        raise QualificationError('Manifest must identify this application controller')
    transports = []
    for node, info in controllers.items():
        if node == 'shared' or not isinstance(info, dict) or not info.get('evidence') or not isinstance(info.get('transport'), str) or not info['transport']:
            errors.add('Unverified controller mapping: ' + node)
        else:
            transports.append(info['transport'])
    if len(set(transports)) != len(transports):
        errors.add('Controller transport mapping is ambiguous')
    for table, items in (('provider', providers), ('resource', resources)):
        if not isinstance(items, dict):
            raise QualificationError('Invalid ' + table + ' mapping')
        for key, entry in items.items():
            if not isinstance(entry, dict) or entry.get('owner') not in controllers or not entry.get('evidence'):
                errors.add('Unverified ' + table + ' ownership: ' + key)
    for resource, entry in resources.items():
        if isinstance(entry, dict):
            provider = providers.get(entry.get('provider'), {})
            if not provider or provider.get('owner') != entry.get('owner'):
                errors.add('Resource/provider owner mismatch: ' + resource)
    assignments = manifest.get('records', {})
    if not isinstance(assignments, dict):
        raise QualificationError('Invalid record assignments')
    for kind, values in assignments.items():
        if not isinstance(values, dict):
            raise QualificationError('Invalid record assignments')
        for key, owner in values.items():
            if (kind, key) not in records:
                errors.add('Stale record assignment: ' + kind + '/' + key)
    for ref, value in records.items():
        kind, key = ref
        owner = assignments.get(kind, {}).get(key)
        if isinstance(records, CandidateSnapshot) and ref in records.overlay and owner is None:
            if kind == '_retention':
                owner = assignments.get(value.get('kind'), {}).get(value.get('key'))
            elif ref == ('_meta', 'operational_retention'):
                owner = 'shared'
        if owner not in controllers and owner != 'shared':
            errors.add('Missing single record owner: ' + kind + '/' + key)
            continue
        if kind not in SHARED_KINDS | OWNED_KINDS and ref not in qualified_repairs:
            errors.add('Unqualified record kind: ' + kind)
        virtual_offer = kind == 'offers' and isinstance(value, dict) and value.get('provider') in VIRTUAL
        shared_marker = (kind == '_retention' and isinstance(value, dict) and
                         assignments.get(value.get('kind'), {}).get(value.get('key')) == 'shared')
        if owner == 'shared' and not (kind in SHARED_KINDS or virtual_offer or shared_marker or kind == '_meta' and key in SHARED_META):
            errors.add('Executable or authority record cannot be shared: ' + kind + '/' + key)
        owners[ref] = owner

    # Bounded scalar indexes avoid repeated full-payload scans for each guard,
    # queue entry, subscription or active graph. No payload snapshots are kept.
    by_id, lease_guards, offers_by_contract, event_topics = {}, {}, {}, {}
    for ref in records:
        by_id.setdefault(ref[1], set()).add(ref)
    for lease_ref, lease in records.kind_items('leases'):
        if isinstance(lease, dict) and isinstance(lease.get('guard'), str):
            lease_guards.setdefault(lease['guard'], []).append(lease_ref[1])
    for offer_ref, offer in records.kind_items('offers'):
        if isinstance(offer, dict) and isinstance(offer.get('contract'), str):
            offers_by_contract.setdefault(offer['contract'], []).append((offer_ref, {
                field: offer.get(field) for field in ('resource', 'provider', 'state', 'service')}))
    for event_ref, event in records.kind_items('events'):
        if not isinstance(event, dict) or type(event.get('seq')) is not int or not isinstance(event.get('topic'), str):
            errors.add('Invalid event delivery record: ' + event_ref[1])
            continue
        for topic in set((event['topic'], '*')):
            event_topics.setdefault(topic, []).append((event['seq'], event_ref[1]))
    for events in event_topics.values():
        events.sort()
    checked_dependencies = [0]
    dependency_limit = manifest.get('dependency_limit', DEPENDENCY_LIMIT)
    if type(dependency_limit) is not int or dependency_limit < 1:
        raise QualificationError('Invalid dependency inventory bound')

    def pending_events(topic, cursor):
        if type(cursor) is not int:
            raise QualificationError('Invalid event cursor')
        events = event_topics.get(topic, [])
        start = bisect.bisect_right(events, (cursor, chr(0x10ffff)))
        for index in range(start, len(events)):
            yield events[index][1]

    def need(ref, kind, key, required=True):
        checked_dependencies[0] += 1
        if checked_dependencies[0] > dependency_limit:
            raise QualificationError('Dependency inventory exceeds configured safety bound')
        if not key:
            if required:
                errors.add('Missing dependency on ' + kind + ': ' + '/'.join(ref))
            return
        if not isinstance(key, str):
            errors.add('Invalid dependency identifier for ' + '/'.join(ref))
            return
        target = (kind, key)
        if target not in records:
            errors.add('Missing dependency ' + kind + '/' + str(key) + ' for ' + '/'.join(ref))
        elif owners.get(target) not in (owners.get(ref), 'shared'):
            errors.add('Cross-owner dependency ' + '/'.join(ref) + ' -> ' + '/'.join(target))

    def physical(ref, provider, resource):
        if provider in VIRTUAL:
            return
        p, r = providers.get(provider, {}), resources.get(resource, {})
        if not p or not r or r.get('provider') != provider or p.get('owner') != r.get('owner') or owners.get(ref) != p.get('owner'):
            errors.add('Unverified provider/resource/action owner: ' + '/'.join(ref))
        auth = p.get('authentication', {})
        if not isinstance(auth, dict) or auth.get('realm') != 'site' or not auth.get('evidence'):
            errors.add('Existing provider authentication path is unqualified: ' + str(provider))
        elif auth.get('mode') == 'native_peer':
            # The native signed-provider API explicitly checks actor==provider.
            # This does not apply to existing OAuth gateway operator transport.
            actor = auth.get('actor')
            if actor != provider:
                errors.add('Native signed actor does not satisfy provider.owner: ' + str(provider))
            need(ref, 'peers', actor)
        elif auth.get('mode') == 'gateway_oauth':
            endpoint = auth.get('endpoint')
            parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
            if not parsed or parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                errors.add('Existing gateway OAuth endpoint is unqualified: ' + str(provider))
            # Do not invent a native peer or copy OAuth credentials. Actual
            # gateway authentication and explicit owner routing are separately
            # bound by the required native qualification evidence.
        else:
            errors.add('Unsupported provider authentication path: ' + str(provider))

    for ref, value in records.items():
        kind, key = ref
        if kind in ('offers', 'actions', 'leases', 'runs', 'work', 'guards', 'activations') and not isinstance(value, dict):
            errors.add('Invalid domain record: ' + '/'.join(ref))
            continue
        states = {'offers': ('ready', 'available_after_setup', 'offline'),
                  'actions': ('queued', 'claimed', 'uncertain', 'succeeded', 'failed'),
                  'leases': ('queued', 'bound', 'fencing', 'released'),
                  'runs': ('running', 'completed', 'failed', 'cancelled'),
                  'activations': ('pending', 'active', 'failed')}
        if kind in states and value.get('state') not in states[kind]:
            errors.add('Unqualified domain state: ' + '/'.join(ref))
        if kind == 'definitions' and isinstance(value, dict):
            need(ref, 'process_versions', value.get('rev'))
        elif kind == 'process_versions' and isinstance(value, dict):
            for child in value.get('calls', {}).values():
                need(ref, 'process_versions', child)
        elif kind == 'documents' and isinstance(value, dict):
            for head in value.get('heads', []):
                need(ref, 'revisions', head)
        elif kind == 'revisions' and isinstance(value, dict):
            for parent in value.get('parents', []):
                need(ref, 'revisions', parent)
        elif kind == 'events':
            if owners.get(('_meta', 'events')) != 'shared':
                errors.add('Current event high water must accompany each controller')
        elif kind == 'mission':
            if not inert_mission(key, value):
                errors.add('Mission is not the qualified inert allocation schema: ' + '/'.join(ref))
            else:
                for offer_ref, offer in records.items():
                    if offer_ref[0] == 'offers' and offer.get('service') in value['allocation'] and owners.get(offer_ref) != owners.get(ref):
                        errors.add('Mission allocation spans owners; qualify per-owner policy before migration: ' + '/'.join(offer_ref))
        elif kind == 'offers':
            need(ref, 'contracts', value.get('contract'))
            physical(ref, value.get('provider'), value.get('resource'))
            if value.get('provider') not in VIRTUAL:
                need(ref, '_enforcers', value.get('resource'))
        elif kind in ('actions', 'leases'):
            need(ref, 'contracts', value.get('contract'))
            if kind == 'actions':
                need(ref, 'leases', value.get('lease'))
                physical(ref, value.get('provider'), value.get('resource'))
                lease = records.get(('leases', value.get('lease')), {})
                for field in ('provider', 'resource', 'guard', 'generation', 'contract'):
                    if lease.get(field) != value.get(field):
                        errors.add('Action/lease binding mismatch: ' + key + '/' + field)
                if value.get('state') not in ('succeeded', 'failed') and lease.get('state') not in ('bound', 'fencing'):
                    errors.add('Unresolved action has no live fence: ' + key)
                need(ref, 'runs', value.get('child'), False)
                need(ref, 'decisions', value.get('decision_id'), False)
            elif value.get('state') == 'queued':
                need(ref, '_lease_queue', value.get('queue_key'))
                resource = value.get('requested_resource')
                if resource and resources.get(resource, {}).get('owner') != owners.get(ref):
                    errors.add('Queued lease resource ownership unresolved: ' + key)
                if not resource:
                    eligible = any(owners.get(r) in (owners.get(ref), 'shared') and o.get('state') == 'ready'
                                   for r, o in offers_by_contract.get(value.get('contract'), []))
                    if not eligible:
                        errors.add('Queued lease has no verified local offer: ' + key)
            elif value.get('state') in ('bound', 'fencing'):
                physical(ref, value.get('provider'), value.get('resource'))
                need(ref, 'offers', value.get('offer'))
                need(ref, 'guards', value.get('guard'))
                guard = records.get(('guards', value.get('guard')), {})
                if guard.get('holder') != key or guard.get('generation') != value.get('generation'):
                    errors.add('Live lease fence mismatch: ' + key)
        elif kind == 'guards':
            need(ref, 'leases', value.get('holder'), False)
            # Slot syntax is the existing fence protocol, not an ownership
            # heuristic. Its resource must still have a verified explicit map.
            resource = key
            if key.startswith('!slot:'):
                resource, separator, slot = key[6:].rpartition(':')
                if not separator or not slot.isdigit():
                    errors.add('Invalid slot guard identifier: ' + key)
            if resource in resources and resources[resource].get('owner') != owners.get(ref):
                errors.add('Guard generation would leave its resource owner: ' + key)
            for lease_id in lease_guards.get(key, []):
                need(ref, 'leases', lease_id)
        elif kind == 'activations':
            need(ref, 'offers', value.get('offer'))
            physical(ref, value.get('provider'), value.get('resource'))
        elif kind == 'runs':
            need(ref, 'process_versions', value.get('rev'))
            need(ref, 'work', value.get('task'), False)
            parent = value.get('parent')
            if parent:
                need(ref, 'actions' if parent.startswith('action:') else 'runs', parent[7:] if parent.startswith('action:') else parent)
            for child in value.get('children', []):
                need(ref, 'runs', child)
            for token in value.get('tokens', []):
                for field, target in (('child', 'runs'), ('action', 'actions'), ('wait', 'waits'), ('activity_lease', 'leases'), ('queued_lease', 'leases')):
                    need(ref, target, token.get(field), False)
            if value.get('state') == 'running':
                _check_run_routes(ref, value.get('rev'), records, owners, resources, errors,
                                  by_id, offers_by_contract)
        elif kind == 'work':
            need(ref, 'work', value.get('parent'), False)
            for dependency in value.get('dependencies', []):
                need(ref, 'work', dependency)
            # Expired terminal attempts are allowed, but surviving ones co-locate.
            for attempt in value.get('attempts', []):
                if ('runs', attempt) in records:
                    need(ref, 'runs', attempt)
        elif kind == '_enforcers':
            physical(ref, value, key)
        elif kind == '_capacity' and key in resources:
            if resources[key].get('owner') != owners.get(ref):
                errors.add('Capacity owner mismatch: ' + key)
        elif kind == '_capacity':
            offers = [r for r, item in records.items() if r[0] == 'offers' and item.get('resource') == key]
            if not offers or any(owners.get(r) not in (owners.get(ref), 'shared') for r in offers):
                errors.add('Capacity has no qualified resource owner: ' + key)
        elif kind == '_offer_usage':
            need(ref, 'offers', key)
        elif kind == '_lease_queue':
            need(ref, 'leases', value)
            lease = records.get(('leases', value), {}) if isinstance(value, str) else {}
            if lease.get('state') != 'queued' or lease.get('queue_key') != key:
                errors.add('Lease queue binding mismatch: ' + key)
        elif kind in ('_submission_active', '_submissions'):
            if kind == '_submission_active':
                need(ref, '_submissions', key)
            elif isinstance(value, dict):
                need(ref, 'leases', value.get('lease'))
        elif kind == '_submission_leases':
            need(ref, 'leases', key)
            need(ref, '_submissions', value)
        elif kind == '_reservation_activity':
            need(ref, 'leases', key)
        elif kind == 'subscriptions' and isinstance(value, dict):
            need(ref, 'definitions', value.get('process'), False)
            for event_id in pending_events(value.get('topic'), value.get('cursor', 0)):
                need(ref, 'events', event_id)
        elif kind == 'assistant_wake_outbox' and isinstance(value, dict):
            for topic in value.get('topics', []):
                for event_id in pending_events(topic, value.get('cursor', 0)):
                    need(ref, 'events', event_id)
        elif kind in ('waits', 'incoming') and isinstance(value, dict):
            need(ref, 'runs', value.get('run'))
        elif kind == 'grants' and isinstance(value, dict):
            need(ref, 'work', value.get('task'))
            need(ref, 'process_versions', value.get('rev'))
            need(ref, 'peers', value.get('to'))
        elif kind == '_authorization_protected' and isinstance(value, dict):
            resource = resources.get(value.get('resource'), {})
            if not resource or resource.get('owner') != owners.get(ref):
                errors.add('Authorization protection must remain with its physical resource: ' + key)
        elif kind == 'authorization_usage' and isinstance(value, dict):
            need(ref, 'authorization_grants', value.get('authorization_id'))
            if ('actions', key) in records:
                need(ref, 'actions', key)
        elif kind == 'peers' and key in providers:
            if providers[key].get('owner') != owners.get(ref):
                errors.add('Provider credential would be admitted on the wrong controller: ' + key)
        elif kind == '_retention' and isinstance(value, dict):
            target = (value.get('kind'), value.get('key'))
            if target in records:
                need(ref, *target)
                if owners.get(ref) != owners.get(target):
                    errors.add('Retention eligibility must accompany every copied record: ' + '/'.join(ref))
        if kind.startswith('assistant_') and kind != 'assistant_config':
            need(ref, 'assistant_config', 'current')

    # Conservative closure for nested business/assistant/recovery references.
    # Reference-only shared documents can describe work at either owner.
    # Explicit domain checks above detect absent mandatory dependencies too.
    for ref, value in records.items():
        if owners.get(ref) == 'shared' or ref[0] in ('_meta', '_retention'):
            continue
        body = {k: v for k, v in value.items() if k != 'id'} if isinstance(value, dict) else value
        for word in set(strings(body)):
            matches = by_id.get(word, set()) - {ref}
            for target in matches:
                if owners.get(target) not in (owners.get(ref), 'shared'):
                    errors.add('Unresolved/ambiguous reference ' + '/'.join(ref) + ' -> ' + '/'.join(target))

    qualification = manifest.get('qualifications', {})
    missing = [name for name in QUALIFICATIONS if not isinstance(qualification.get(name), str) or not qualification[name].strip()]
    for name in missing:
        errors.add('Native qualification required: ' + name)
    target = config['node']
    counts = {'owned': 0, 'shared': 0, 'foreign_offline': 0, 'other_owner': 0}
    for ref, value in records.items():
        owner = owners.get(ref)
        disposition = 'shared' if owner == 'shared' else 'owned' if owner == target else 'foreign_offline' if ref[0] == 'offers' else 'other_owner'
        counts[disposition] += 1
        changes.append({'kind': ref[0], 'id': ref[1], 'owner': owner, 'disposition': disposition, 'sha256': sha(value)})
    plan = {'format': 1, 'site': config['site'], 'node': target, 'source': os.path.basename(path),
            'source_inventory': summary['sha256'], 'source_position': summary['position'],
            'config_sha256': sha(config), 'manifest_sha256': sha(manifest),
            'candidate': 'site.sqlite', 'counts': counts, 'records': changes,
            'blockers': sorted(errors), 'ready_to_stage': not errors, 'activation_performed': False,
            'preserve_in_place': ['node.json', 'node.key', 'local.sqlite', 'provider identities and fence/operation ledgers', 'local event delivery store'],
            'post_adoption_retirement': retirement_inventory(os.path.dirname(path), config['site']),
            'configuration_change_required': {'remove': ['gateway_journal', 'etcd'], 'gateway_link_controller_id': controllers[target].get('transport')}}
    if retention_selection is not None:
        plan['retention_selection'] = retention_selection
        plan['counts']['omitted_history'] = len(retention_selection['omitted'])
    plan['sha256'] = sha(plan)
    return plan


def _check_run_routes(ref, rev, records, owners, resources, errors, by_id, offers_by_contract, seen=None):
    seen = set() if seen is None else seen
    if rev in seen:
        return
    seen.add(rev)
    version = records.get(('process_versions', rev), {})
    if owners.get(('process_versions', rev)) not in (owners.get(ref), 'shared'):
        errors.add('Active process revision belongs to another owner: ' + '/'.join(ref))
    # Future literal dependencies inside the pinned graph matter too, not just
    # references already materialized in a current token.
    graph_strings = set(strings(version.get('graph', {})))
    for word in graph_strings:
        for target in by_id.get(word, ()):
            if owners.get(target) not in (owners.get(ref), 'shared'):
                errors.add('Active graph references another owner: ' + '/'.join(ref) + ' -> ' + '/'.join(target))
    for node in version.get('graph', {}).get('nodes', {}).values():
        if node.get('type') == 'service' and node.get('contract'):
            contract, resource = node['contract'], node.get('resource', '')
            if not isinstance(contract, str) or not isinstance(resource, str):
                errors.add('Active dynamic capability route requires reconciliation before migration: ' + '/'.join(ref))
                continue
            available = any((not resource or o.get('resource') == resource) and owners.get(r) in (owners.get(ref), 'shared')
                            for r, o in offers_by_contract.get(contract, []))
            if not available or resource in resources and resources[resource].get('owner') != owners.get(ref):
                errors.add('Active process depends on a foreign/unverified capability: ' + '/'.join(ref) + '/' + contract)
    for child in version.get('calls', {}).values():
        if ('process_versions', child) not in records:
            errors.add('Missing process revision dependency: ' + str(child))
        else:
            _check_run_routes(ref, child, records, owners, resources, errors, by_id, offers_by_contract, seen)


def plan(home, manifest, limits=None):
    with snapshot(home, limits) as inputs:
        records, selected = retained_snapshot(inputs[2], manifest)
        return _plan(inputs[0], inputs[1], records, inputs[3], manifest, selected)


def stage(home, manifest, output, expected_plan_sha256, limits=None):
    """Write one NEW review candidate; never activate or overwrite existing data."""
    output = os.path.abspath(output)
    home = os.path.realpath(home)
    if os.path.realpath(output) == home or os.path.realpath(output).startswith(home + os.sep):
        raise QualificationError('Candidate must be outside controller home; activation is separate')
    if os.path.basename(output) != 'site.sqlite':
        raise QualificationError('Candidate must use the existing standalone name site.sqlite')
    directory_path = os.path.dirname(output)
    if os.path.realpath(output) != output:
        raise QualificationError('Candidate path must be canonical with no symlink components')
    directory_info = os.stat(directory_path)
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid() or directory_info.st_mode & 0o077:
        raise QualificationError('Staging directory must be private and owned by the current user (mode 0700)')
    with snapshot(home, limits) as inputs:
        records, selected = retained_snapshot(inputs[2], manifest)
        current = _plan(inputs[0], inputs[1], records, inputs[3], manifest, selected)
        if current['sha256'] != expected_plan_sha256:
            raise QualificationError('Plan/source/config changed; inspect a fresh plan')
        if current['blockers']:
            raise QualificationError('Migration is not qualified: ' + '; '.join(current['blockers'][:8]))
        byte_limit = dict(DEFAULT_LIMITS, **(limits or {}))['bytes']
        if byte_limit < 16384:
            raise QualificationError('Candidate disk bound is smaller than minimal SQLite schema')
        fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        created = os.fstat(fd)

        def same_output():
            try:
                info = os.lstat(output)
                directory = os.stat(directory_path)
                return (stat.S_ISREG(info.st_mode) and
                        (info.st_dev, info.st_ino) == (created.st_dev, created.st_ino) and
                        (directory.st_dev, directory.st_ino) == (directory_info.st_dev, directory_info.st_ino))
            except OSError:
                return False

        db = None
        try:
            if not same_output():
                raise QualificationError('Candidate path changed before SQLite open')
            db = sqlite3.connect(output)
            if not same_output():
                raise QualificationError('Candidate path changed during SQLite open')
            db.execute('PRAGMA page_size=4096')
            db.execute('PRAGMA max_page_count=' + str(byte_limit // 4096))
            db.execute('PRAGMA cache_size=-1024')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id));'
                             'CREATE TABLE position(id INTEGER PRIMARY KEY CHECK(id=1),seq INTEGER,hash TEXT);')
            db.execute("INSERT INTO position VALUES(1,0,'')")
            count, selected_bytes = 0, 0
            for item in current['records']:
                if item['disposition'] == 'other_owner':
                    continue
                value = records[(item['kind'], item['id'])]
                if item['disposition'] == 'foreign_offline':
                    value = dict(value, state='offline')
                raw = encode(value)
                selected_bytes += len(raw.encode('utf-8')) + len(item['kind'].encode('utf-8')) + len(item['id'].encode('utf-8'))
                if selected_bytes > byte_limit:
                    raise QualificationError('Selected canonical records exceed candidate byte bound')
                db.execute('INSERT INTO records VALUES(?,?,?)', (item['kind'], item['id'], raw))
                count += 1
            db.commit()
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise QualificationError('Candidate integrity check failed')
            db.close()
            db = None
            if not same_output():
                raise QualificationError('Candidate path changed during staging')
            os.fsync(fd)
            directory = os.open(directory_path, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            if db is not None:
                db.close()
            # Never remove a concurrently substituted file or follow a link.
            if same_output():
                os.unlink(output)
            raise
        finally:
            os.close(fd)
        return {'candidate': output, 'records': count, 'plan_sha256': current['sha256'],
                'activation_performed': False, 'node_config_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inventory', 'retention', 'plan', 'stage'))
    parser.add_argument('--home', required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--output')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--at', type=float, help='fixed reviewed retention evaluation time')
    parser.add_argument('--seconds', type=float, default=86400)
    parser.add_argument('--keep', type=int, default=1024)
    parser.add_argument('--max-dependencies', type=int, default=DEPENDENCY_LIMIT)
    parser.add_argument('--repair-review', help='qualified completed-repair schema/reader evidence JSON')
    parser.add_argument('--max-rows', type=int, default=DEFAULT_LIMITS['rows'])
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_LIMITS['bytes'])
    parser.add_argument('--max-row-bytes', type=int, default=DEFAULT_LIMITS['row_bytes'])
    args = parser.parse_args()
    limits = {'rows': args.max_rows, 'bytes': args.max_bytes, 'row_bytes': args.max_row_bytes}
    try:
        if args.command == 'inventory':
            result = inventory(args.home, limits)
        elif args.command == 'retention':
            if args.at is None:
                parser.error('--at is required for deterministic retention selection')
            from biomerte.retention_selection import plan as select
            with snapshot(args.home, limits) as inputs:
                result = select(inputs[2], args.at, seconds=args.seconds, keep=args.keep,
                                dependency_limit=args.max_dependencies,
                                repair_review=load_json(args.repair_review) if args.repair_review else None)
        else:
            if not args.manifest:
                parser.error('--manifest is required')
            manifest = load_json(args.manifest)
            if args.command == 'plan':
                result = plan(args.home, manifest, limits)
            else:
                if not args.output or not args.plan_sha256:
                    parser.error('--output and --plan-sha256 are required')
                result = stage(args.home, manifest, args.output, args.plan_sha256, limits)
        print(encode(result))
    except (QualificationError, sqlite3.Error, OSError, ValueError) as exc:
        print(encode({'error': str(exc), 'activation_performed': False}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
