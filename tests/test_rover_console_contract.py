"""Check the deployed console's declared reads against the producer contract."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path('providers/robot-vision/rover-stream-schemas.json')
READS = ROOT / 'tests' / 'rover-console-stream-reads.json'


def dereference(node, document):
    while '$ref' in node:
        node = document['$defs'][node['$ref'].rsplit('/', 1)[-1]]
    return node


def read_path(document, path):
    node = document
    for key in path:
        node = dereference(node, document)
        node = node.get('items') if key == '[]' else node.get('properties', {}).get(key)
        if node is None:
            return False
    return True


def compatible(previous, current, before, after, path):
    previous = dereference(previous, before)
    current = dereference(current, after)
    keys = set(previous) | set(current)
    for key in sorted(keys - {'$defs', '$id', 'version', 'description', 'title', 'properties', 'items', 'required'}):
        if previous.get(key) != current.get(key):
            raise ValueError(f'Constraint changed at {path}.{key}')
    if set(current.get('required', [])) - set(previous.get('required', [])):
        raise ValueError(f'Required field added at {path}')
    changed = previous.get('required', []) != current.get('required', [])
    old_fields = previous.get('properties', {})
    new_fields = current.get('properties', {})
    for name, old in old_fields.items():
        if name not in new_fields:
            raise ValueError(f'Field removed at {path}.{name}')
        if compatible(old, new_fields[name], before, after, path + '.' + name):
            changed = True
    if previous.get('items') is not None:
        if 'items' not in current:
            raise ValueError(f'Array item schema removed at {path}')
        if compatible(previous['items'], current['items'], before, after, path + '[]'):
            changed = True
    return changed or set(old_fields) != set(new_fields) or ('items' in current and 'items' not in previous)


def evolution(old, new):
    for schema_id, before in old.items():
        if schema_id not in new:
            raise ValueError(f'Old major must stay published: {schema_id}')
        after = new[schema_id]
        changed = compatible(before, after, before, after, schema_id)
        v1, v2 = before['version'], after['version']
        if v1['major'] != v2['major'] or v2['minor'] < v1['minor'] + int(changed):
            raise ValueError(f'Expected compatible minor bump at {schema_id}')


class PublishedConsumerContract(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / SCHEMA).read_text())

    def test_console_reads(self):
        paths = json.loads(READS.read_text())
        self.assertTrue(paths)
        for row in paths:
            with self.subTest(row=row):
                self.assertTrue(all(read_path(self.catalog[k], row['path']) for k in row['schemas']))

    def test_console_only_field_fails(self):
        frame = self.catalog['rover.vision.frame/1']
        self.assertFalse(read_path(frame, ['tof', 'console_only_field']))

    def test_compatible_change_needs_minor_and_old_major_is_retained(self):
        old = {'example/1': {'properties': {'id': {'type': 'string'}}, 'version': {'major': 1, 'minor': 0}}}
        new = copy.deepcopy(old)
        new['example/1']['properties']['rate'] = {'type': 'number'}
        with self.assertRaises(ValueError):
            evolution(old, new)
        new['example/1']['version']['minor'] = 1
        evolution(old, new)
        new['example/1']['properties']['id'] = {'type': 'integer'}
        with self.assertRaises(ValueError):
            evolution(old, new)
        with self.assertRaises(ValueError):
            evolution(old, {})

    @unittest.skipUnless(os.environ.get('ROVER_CONTRACT_BASE'), 'PR base is supplied by the contract CI job')
    def test_catalog_against_pr_base(self):
        base = os.environ['ROVER_CONTRACT_BASE']
        subprocess.run(['git', 'cat-file', '-e', base + '^{commit}'], cwd=ROOT, check=True)
        names = subprocess.check_output(['git', 'ls-tree', '--name-only', base, '--', str(SCHEMA)], cwd=ROOT, text=True)
        if names.strip():
            data = subprocess.check_output(['git', 'show', base + ':' + str(SCHEMA)], cwd=ROOT)
            evolution(json.loads(data), self.catalog)
