"""Request placement is independent of capability identity and execution inputs."""
from .store import Fault, digest
from .api import ident

FIELDS = ('provider', 'host', 'resource')

def normalize(resource='', placement=None):
    if placement is None:
        placement = {}
    if not isinstance(placement, dict) or set(placement) - set(FIELDS):
        raise Fault('Placement supports provider, host and resource only')
    selected = {}
    for key, value in placement.items():
        if not isinstance(value, str) or not value or len(value) > 255:
            raise Fault('Placement values must be nonempty strings of at most 255 characters')
        if key == 'host':
            value = value.lower().rstrip('.')
            if not value or any(c.isspace() for c in value):
                raise Fault('Invalid placement host')
        else:
            ident(value)
        selected[key] = value
    if resource:
        ident(resource)
        if selected.get('resource', resource) != resource:
            raise Fault('Conflicting placement resource')
    resource = selected.pop('resource', resource)
    return resource, selected

def identity(task, contract, resource, placement):
    # Preserve all existing lease/submission IDs when no new selector is used.
    values = [task, contract, resource]
    if placement:
        values.append(placement)
    return digest(values)

def host(offer):
    config = offer.get('configuration', {})
    return config.get('execution_host') or config.get('host') or ''

def matches(offer, resource='', placement=None):
    placement = placement or {}
    if resource and offer['resource'] != resource:
        return False
    if placement.get('provider') and offer['provider'] != placement['provider']:
        return False
    if placement.get('host'):
        actual = host(offer)
        if not isinstance(actual, str) or actual.lower().rstrip('.') != placement['host']:
            return False
    return True

def summary(offer):
    return dict(provider=offer['provider'], resource=offer['resource'], host=host(offer))

GUIDANCE = {
    'default': 'Omit resource and placement to let the controller select an eligible implementation of this contract.',
    'constraints': 'Optional placement accepts provider, host and resource. All supplied constraints must match; unavailable targets queue without fallback.',
    'host': 'Host is the advertised execution host, never a name inferred from a resource ID. Unknown hosts do not match a host constraint.',
    'affinity': 'Use resource for a particular workspace, storage volume or attached device; use host only when the execution machine matters.',
    'binding': 'Selection is retained by the lease and operation. Reconnects and retries never move an existing execution to another provider.'
}
