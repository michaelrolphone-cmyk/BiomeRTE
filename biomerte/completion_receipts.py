"""Fixed-lifetime completion digests; never restore expired output or execute work."""
import time
from .store import Fault, digest

KIND = '_completion_receipts'
RETENTION = 172800


def remember(tx, action):
    if action.get('contract') not in ('host.terminal.run:1', 'docker.cli:1'):
        return
    outcome = action.get('execution_state', action.get('state'))
    if outcome not in ('succeeded', 'failed') or not action.get('expires_at'):
        return
    expires = action['expires_at'] + RETENTION
    if expires <= time.time():
        return
    from .retention import compact_action
    # Reuse the existing exact identity/digest and proven-quiescence format.
    if action.get('result_expired'):
        receipt = {k:action[k] for k in ('id','state','provider','lease','contract','resource',
                   'generation','guard','identity_digest','result_digest','recovery_result') if k in action}
    else:
        receipt = compact_action(action)
    receipt.update(result_expired=True, acknowledged=True, expires_at=expires)
    if action.get('execution_state'):
        receipt['execution_state'] = outcome
    old = tx.get(KIND, action['id'])
    if old:
        if old != receipt:
            raise Fault('Conflicting retained completion receipt', 409)
        return
    tx.put(KIND, action['id'], receipt)


def find(tx, operation):
    receipt = tx.get(KIND, operation)
    return receipt if receipt and receipt['expires_at'] > time.time() else None


def acknowledge(tx, provider, operation, generation, state, result):
    receipt = find(tx, operation)
    if not receipt:
        raise Fault('No retained authoritative completion; reconcile original executor evidence', 404)
    if ((receipt['provider'], receipt['generation']) != (provider, generation) or
            receipt.get('execution_state', receipt['state']) != state or
            receipt['result_digest'] != digest(result)):
        raise Fault('Conflicting retained completion', 409)
    return receipt
