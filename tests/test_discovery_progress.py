import tempfile, unittest
from unittest.mock import patch
from biomerte import discovery
from biomerte.store import Fault

class Tx:
    def __init__(self, data=None): self.data=data or {}
    def rows(self, kind): return self.data.get(kind, [])
class Discovery(unittest.TestCase):
    def test_operations_page_uses_caller_permissions(self):
        from types import SimpleNamespace
        result=discovery.page(Tx(),SimpleNamespace(allowed=lambda name:name=='system.info'))
        self.assertEqual([x['name'] for x in result['items']], ['system.info'])

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
        tx=Tx({'actions':[dict(state='uncertain',provider='p',contract='c')]})
        self.assertEqual(discovery.progress(tx,None)['state'],'degraded')
    def test_assistant_business_failure_is_visible(self):
        tx=Tx({'assistant_responsibilities':[{'id':'r'}]})
        with patch('biomerte.assistant_support.assistant_status',return_value=dict(id='r',desired='run',state='blocked',active_cycle=None,blocked_dispatches=[{'result_code':'operation_failed'}])):
            result=discovery.progress(tx,None)
        self.assertEqual(result['state'],'degraded')
        self.assertEqual(result['assistants'][0]['state'],'blocked')
