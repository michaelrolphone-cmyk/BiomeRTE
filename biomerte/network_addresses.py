"""Current interface addresses for authenticated controller registration.

Uses the operating system's interface inventory, without DNS, probes or external
address services. The records describe assigned addresses, not reachability.
Compatible with the Raspberry Pi's Python 3.4 and the macOS gateway link.
"""
import ctypes
import ipaddress
import socket
import struct
import sys


class _Ifaddrs(ctypes.Structure):
    pass


_Ifaddrs._fields_ = [
    ('next', ctypes.POINTER(_Ifaddrs)),
    ('name', ctypes.c_char_p),
    ('flags', ctypes.c_uint),
    ('address', ctypes.c_void_p),
    ('netmask', ctypes.c_void_p),
    ('destination', ctypes.c_void_p),
    ('data', ctypes.c_void_p),
]

_PRIVATE_V4 = tuple(ipaddress.ip_network(value) for value in
                    ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
_PRIVATE_V6 = ipaddress.ip_network('fc00::/7')
_SHARED_V4 = ipaddress.ip_network('100.64.0.0/10')


def address_scope(address):
    if address.is_link_local:
        return 'link_local'
    if address.version == 4:
        if any(address in network for network in _PRIVATE_V4):
            return 'private'
        if address in _SHARED_V4:
            return 'shared'
    elif address in _PRIVATE_V6:
        return 'private'
    # is_private includes special-use blocks as well as private networks. Keep
    # these separate so documentation and benchmarking ranges are not published
    # as usable private addresses, nor assumed to be public endpoints.
    if not address.is_private and not address.is_reserved:
        return 'public'
    return 'special'


def _sockaddr(pointer, platform=None):
    platform = sys.platform if platform is None else platform
    header = ctypes.string_at(pointer, 2)
    family = header[1] if platform == 'darwin' else struct.unpack('=H', header)[0]
    if family == socket.AF_INET:
        return family, ctypes.string_at(pointer, 8)[4:8], 0
    if family == socket.AF_INET6:
        value = ctypes.string_at(pointer, 28)
        packed = value[8:24]
        scope_id = struct.unpack('=I', value[24:28])[0]
        # BSD kernels may retain the interface index in the link-local address
        # itself. The zone is represented separately and must not alter the IP.
        if platform == 'darwin' and packed[0] == 254 and packed[1] & 192 == 128:
            packed = packed[:2] + b'\0\0' + packed[4:]
        return family, packed, scope_id
    return None


def _interface_addresses():
    if sys.platform != 'darwin' and not sys.platform.startswith('linux'):
        raise OSError('Interface address reporting requires Linux or macOS')
    library = ctypes.CDLL(None, use_errno=True)
    getifaddrs = library.getifaddrs
    getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_Ifaddrs))]
    getifaddrs.restype = ctypes.c_int
    freeifaddrs = library.freeifaddrs
    freeifaddrs.argtypes = [ctypes.POINTER(_Ifaddrs)]
    freeifaddrs.restype = None
    head = ctypes.POINTER(_Ifaddrs)()
    if getifaddrs(ctypes.byref(head)) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot enumerate interface addresses')
    rows = []
    try:
        pointer = head
        while pointer:
            item = pointer.contents
            if (item.address and item.name and item.flags & 1
                    and not item.flags & 8):  # IFF_UP, excluding IFF_LOOPBACK
                value = _sockaddr(item.address)
                if value is not None:
                    family, packed, scope_id = value
                    mask = None
                    if item.netmask:
                        offset, size = (4, 4) if family == socket.AF_INET else (8, 16)
                        mask = ctypes.string_at(item.netmask, offset + size)[offset:]
                    rows.append((item.name.decode('utf-8', 'replace'), family,
                                 packed, mask, scope_id))
            pointer = item.next
    finally:
        freeifaddrs(head)
    return rows


def _prefix_length(mask):
    if mask is None:
        return None
    bits = ''.join(format(value, '08b') for value in mask)
    if '01' in bits:
        return None
    return bits.count('1')


def local_addresses():
    """Return a fresh, deterministic inventory; never cache a prior address."""
    found = {}
    for interface, family, packed, mask, scope_id in _interface_addresses():
        address = ipaddress.ip_address(packed)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            continue
        scope = address_scope(address)
        text = str(address)
        if family == socket.AF_INET6 and scope == 'link_local':
            text += '%' + interface
        value = {'address': text, 'family': 'ipv4' if family == socket.AF_INET else 'ipv6',
                 'interface': interface, 'scope': scope}
        prefix = _prefix_length(mask)
        if prefix is not None:
            value['prefix_length'] = prefix
            value['network'] = str(ipaddress.ip_network(
                str(address) + '/' + str(prefix), strict=False))
        if family == socket.AF_INET6 and scope == 'link_local' and scope_id:
            value['scope_id'] = scope_id
        found[(interface, family, text)] = value
    return [found[key] for key in sorted(found)]


def registration():
    """Report one complete snapshot or explicitly mark collection unavailable.

    The gateway supplies receipt time and binds the report to the authenticated
    controller. A failed enumeration must never refresh cached address data.
    """
    try:
        addresses = local_addresses()
        if len(addresses) > 128:
            return None
        return {'version': 1, 'addresses': addresses}
    except Exception:
        # Network inventory is supplementary; it must not suppress readiness or
        # prevent the next heartbeat from retrying collection after an OS error.
        return None
