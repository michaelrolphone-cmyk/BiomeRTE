import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
from dashboard_read import read_dashboard_definition

class DashboardReadTests(unittest.TestCase):
    def test_exact_revision_and_workspace_reads_do_not_mutate_records(self):
        with tempfile.TemporaryDirectory() as home:
            db=sqlite3.connect(os.path.join(home,'gateway-test.sqlite'))
            db.execute('CREATE TABLE records(kind TEXT,id TEXT,value TEXT,PRIMARY KEY(kind,id))')
            rows=[('definitions','flow',{'id':'flow','rev':'new'}),('process_versions','old',{'id':'old','name':'flow','graph':{'start':'old-step','nodes':{'old-step':{'type':'service'}}}}),('process_versions','new',{'id':'new','name':'flow','graph':{'start':'new-step','nodes':{'new-step':{'type':'service'}}}}),('runs','run',{'id':'run','name':'flow','rev':'old','state':'running','steps':1,'tokens':[{'node':'old-step','vars':{'secret':'must not be in summary'}}]})]
            for kind,key,value in rows:db.execute('INSERT INTO records VALUES(?,?,?)',(kind,key,json.dumps(value)))
            db.commit();before=db.execute('SELECT * FROM records ORDER BY kind,id').fetchall()
            conf={'_dashboard_home':home}
            exact=read_dashboard_definition({'realm':'site','name':'flow','revision':'old'},conf)
            self.assertEqual(exact['body']['result']['graph']['start'],'old-step')
            current=read_dashboard_definition({'realm':'site','name':'flow'},conf)
            self.assertEqual(current['body']['result']['id'],'new')
            summary=read_dashboard_definition({'realm':'site','mode':'summaries'},conf)['body']['result']['runs']['run']
            self.assertEqual(summary['current_nodes'][0]['id'],'old-step')
            self.assertNotIn('secret',json.dumps(summary))
            self.assertEqual(read_dashboard_definition({'realm':'site','name':'other','revision':'old'},conf)['status'],404)
            self.assertEqual(read_dashboard_definition({'realm':'bad','name':'flow'},conf)['status'],400)
            self.assertEqual(db.execute('SELECT * FROM records ORDER BY kind,id').fetchall(),before)
            db.close()

if __name__=='__main__':unittest.main()
