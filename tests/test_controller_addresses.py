import ctypes
import importlib.util
import ipaddress
import os
import socket
import struct
import sys
import types
import unittest
from unittest.mock import patch


def load():
    path = os.path.join(os.path.dirname(__file__), '..', 'biomerte', 'network_addresses.py')
    spec = importlib.util.spec_from_file_location('network_addresses', path)
    if hasattr(importlib.util, 'module_from_spec'):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:  # Python 3.4 on the Pi
        module = spec.loader.load_module(spec.name)
    return module


class ControllerAddressTests(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def row(self, address, interface='en1', prefix=None, scope_id=0):
        parsed = ipaddress.ip_address(address)
        family = socket.AF_INET if parsed.version == 4 else socket.AF_INET6
        mask = None
        if prefix is not None:
            mask = ipaddress.ip_network(address + '/' + str(prefix), strict=False).netmask.packed
        return interface, family, parsed.packed, mask, scope_id

    def test_private_public_and_shared_networks_remain_distinct(self):
        cases = {'10.2.3.4': 'private', '172.20.1.9': 'private',
                 '192.168.1.55': 'private', 'fd12:3456::8': 'private',
                 '8.8.8.8': 'public', '2606:4700:4700::1111': 'public',
                 '100.64.12.3': 'shared', '169.254.2.3': 'link_local',
                 'fe80::7': 'link_local', '192.0.2.4': 'special',
                 '2001:db8::1': 'special'}
        with patch.object(self.module, '_interface_addresses',
                          return_value=[self.row(value) for value in cases]):
            result = self.module.local_addresses()
        self.assertEqual(len(result), len(cases))
        for value in result:
            self.assertEqual(value['scope'], cases[value['address'].split('%')[0]])

    def test_ipv6_link_local_keeps_interface_zone_and_prefix(self):
        rows = [self.row('fe80::abcd', 'en1', 64, 5),
                self.row('fe80::abcd', 'utun2', 64, 8)]
        with patch.object(self.module, '_interface_addresses', return_value=rows):
            values = self.module.local_addresses()
        self.assertEqual([value['address'] for value in values],
                         ['fe80::abcd%en1', 'fe80::abcd%utun2'])
        self.assertEqual([value['scope_id'] for value in values], [5, 8])
        self.assertEqual(values[0]['prefix_length'], 64)
        self.assertEqual(values[0]['network'], 'fe80::/64')

    def test_each_snapshot_replaces_changed_and_removed_addresses(self):
        first = [self.row('192.168.1.23', 'eth0', 24)]
        second = [self.row('192.168.1.81', 'eth0', 24),
                  self.row('fd12::81', 'eth0', 64)]
        with patch.object(self.module, '_interface_addresses',
                          side_effect=[first, second, []]):
            before = self.module.local_addresses()
            after = self.module.local_addresses()
            gone = self.module.local_addresses()
        self.assertEqual(before[0]['address'], '192.168.1.23')
        self.assertEqual(set(value['address'] for value in after),
                         set(('192.168.1.81', 'fd12::81')))
        self.assertEqual(after[0]['network'], '192.168.1.0/24')
        self.assertEqual(gone, [])

    def test_duplicate_and_non_destination_addresses_are_excluded(self):
        rows = [self.row(value) for value in
                ('127.0.0.1', '::1', '0.0.0.0', '::', '224.0.0.1', 'ff02::1',
                 '192.168.1.55', '192.168.1.55')]
        with patch.object(self.module, '_interface_addresses', return_value=rows):
            self.assertEqual(self.module.local_addresses(),
                             [{'address': '192.168.1.55', 'family': 'ipv4',
                               'interface': 'en1', 'scope': 'private'}])

    def test_collection_failure_does_not_reuse_a_previous_snapshot(self):
        with patch.object(self.module, '_interface_addresses',
                          side_effect=[[self.row('192.168.1.55')], OSError('failed')]):
            self.assertEqual(self.module.local_addresses()[0]['address'], '192.168.1.55')
            with self.assertRaises(OSError):
                self.module.local_addresses()

    def test_sockaddr_decodes_native_linux_and_macos_layouts(self):
        for platform in ('linux', 'darwin'):
            for text in ('192.168.1.55', 'fe80::aabb:ccdd:eeff:1234'):
                ip = ipaddress.ip_address(text)
                family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
                size = 16 if ip.version == 4 else 28
                header = bytes((size, family)) if platform == 'darwin' else struct.pack('=H', family)
                body = header + b'\0\0'
                if ip.version == 4:
                    body += ip.packed + b'\0' * 8
                    expected_scope = 0
                else:
                    body += b'\0' * 4 + ip.packed + struct.pack('=I', 5)
                    expected_scope = 5
                buffer = ctypes.create_string_buffer(body)
                self.assertEqual(self.module._sockaddr(ctypes.addressof(buffer), platform),
                                 (family, ip.packed, expected_scope))

    def test_darwin_embedded_link_local_scope_does_not_change_address(self):
        packed = ipaddress.ip_address('fe80::aabb').packed
        embedded = packed[:2] + b'\0\5' + packed[4:]
        body = bytes((28, socket.AF_INET6)) + b'\0' * 6 + embedded + struct.pack('=I', 5)
        buffer = ctypes.create_string_buffer(body)
        self.assertEqual(self.module._sockaddr(ctypes.addressof(buffer), 'darwin'),
                         (socket.AF_INET6, packed, 5))

    def test_registration_reports_unavailable_without_suppressing_retry(self):
        with patch.object(self.module, 'local_addresses',
                          side_effect=[OSError('unavailable'), [], [{'address': '192.168.1.81'}]]):
            self.assertIsNone(self.module.registration())
            self.assertEqual(self.module.registration(), {'version': 1, 'addresses': []})
            self.assertEqual(self.module.registration()['addresses'][0]['address'], '192.168.1.81')

    def test_oversize_inventory_is_not_silently_truncated(self):
        with patch.object(self.module, 'local_addresses', return_value=[{}] * 129):
            self.assertIsNone(self.module.registration())

    def test_native_inventory_excludes_down_and_loopback_interfaces_and_frees_memory(self):
        rows = []
        buffers = []
        for name, flags, text in (('lo0', 9, '169.254.1.1'),
                                  ('eth1', 0, '192.168.2.4'),
                                  ('eth0', 1, '192.168.1.23')):
            header = (bytes((16, socket.AF_INET)) if sys.platform == 'darwin'
                      else struct.pack('=H', socket.AF_INET))
            buffer = ctypes.create_string_buffer(
                header + b'\0\0' + ipaddress.ip_address(text).packed + b'\0' * 8)
            buffers.append(buffer)
            row = self.module._Ifaddrs()
            row.name = name.encode('ascii')
            row.flags = flags
            row.address = ctypes.addressof(buffer)
            rows.append(row)
        for index in range(len(rows) - 1):
            rows[index].next = ctypes.pointer(rows[index + 1])
        released = []
        def getifaddrs(target):
            pointer_type = ctypes.POINTER(ctypes.POINTER(self.module._Ifaddrs))
            ctypes.cast(target, pointer_type)[0] = ctypes.pointer(rows[0])
            return 0
        def freeifaddrs(pointer):
            released.append(bool(pointer))
        library = types.SimpleNamespace(getifaddrs=getifaddrs, freeifaddrs=freeifaddrs)
        with patch.object(self.module.ctypes, 'CDLL', return_value=library):
            result = self.module.local_addresses()
        self.assertEqual(result, [{'address': '192.168.1.23', 'family': 'ipv4',
                                   'interface': 'eth0', 'scope': 'private'}])
        self.assertEqual(released, [True])

    def test_noncontiguous_netmask_is_not_misreported(self):
        self.assertIsNone(self.module._prefix_length(b'\xff\0\xff\0'))


if __name__ == '__main__':
    unittest.main()
