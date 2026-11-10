import tempfile, unittest
from unittest.mock import patch
from biomerte import discovery
from biomerte.store import Fault, Store

class Tx:
    def __init__(self, data=None): self.data=data or {}
    def rows(self, kind): return self.data.get(kind, [])
class Discovery(unittest.TestCase):
    def progress(self, data):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory, 'site')
            def seed(tx):
                for kind, rows in data.items():
                    for i, row in enumerate(rows):
                        tx.put(kind, row.get('id', str(i)), row)
            store.execute(seed)
            return store.execute(lambda tx: discovery.progress(tx, None), read_only=True)

    def test_operations_page_uses_caller_permissions(self):
        from types import SimpleNamespace
        result=discovery.page(Tx(),SimpleNamespace(allowed=lambda name:name=='cap.discover'))
        self.assertEqual([x['name'] for x in result['items']], ['cap.discover'])

    def test_pages_retain_every_item_and_can_progress_across_sections(self):
        rows=[dict(id=str(n).zfill(3),description='x'*9000) for n in range(5)]
        tx=Tx(); seen=[]; cursor=''; section='contracts'
        with patch.object(discovery.providers,'discover',return_value={'contracts':rows,'offers':[]}):
            while True:
                page=discovery.page(tx,None,section,cursor)
                if page['section']=='contracts':seen.extend(x['id'] for x in page['items'])
                if page['complete']:break
                cursor=page['next_cursor'];section=''
        self.assertEqual(seen,[x['id'] for x in rows])
    def test_large_individual_item_is_not_dropped(self):
        row={'id':'large','description':'x'*500000}
        with patch.object(discovery.providers,'discover',return_value={'contracts':[row],'offers':[]}):
            self.assertEqual(discovery.page(Tx(),None,'contracts')['items'],[row])
    def test_bad_cursor_returns_explicit_error(self):
        with self.assertRaises(Fault): discovery.page(Tx(),None,cursor='broken')
    def test_progress_exposes_uncertainty_separately(self):
        result = self.progress({'actions':[dict(state='uncertain',provider='p',contract='c')]})
        self.assertEqual(result['state'], 'degraded')

    def test_assistant_business_failure_is_visible(self):
        result = self.progress({
            'assistant_responsibilities': [{'id':'r', 'desired':'run'}],
            'assistant_dispatch_failures': [{'id':'failure', 'responsibility_id':'r',
                                           'result_code':'operation_failed'}]})
        self.assertEqual(result['state'], 'degraded')
        self.assertEqual(result['assistants'][0]['state'], 'blocked')
