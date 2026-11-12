"""Current-map reads must not repeatedly aggregate durable entity history."""
import tempfile
import unittest
from vision_store import VisionStore

class EntityHeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = VisionStore(self.tmp.name)
        self.mid = self.store.create_map()["map_id"]
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()
    def apply(self, rev, name, *, delete=False, kind="sparse_point"):
        self.store.apply_map_update(self.mid, rev,
            [{"op":"delete" if delete else "upsert", "kind":kind,
              "entity_id":name, "data":{"position":[rev,0,0]}}])
    def test_current_heads_keep_tombstones_and_exact_historical_reads(self):
        self.apply(0,"a");self.apply(1,"b");self.apply(2,"a",delete=True)
        self.assertEqual([e["entity_id"] for e in self.store.read_map(self.mid)["entities"]],["b"])
        self.assertEqual(len(self.store.read_map(self.mid,revision=2)["entities"]),2)
        head=self.store._db.execute("SELECT revision FROM entity_heads WHERE map_id=? "
             "AND kind='sparse_point' AND entity_id='a'",(self.mid,)).fetchone()
        self.assertEqual(head[0],3)
        self.apply(3,"a")
        self.assertEqual(len(self.store.read_map(self.mid)["entities"]),2)
    def test_cursor_stays_at_old_revision_after_external_writer_advances(self):
        self.apply(0,"a");self.apply(1,"b")
        page=self.store.read_map(self.mid,limit=1)
        other=VisionStore(self.tmp.name)
        try:
            other.apply_map_update(self.mid,2,[{"op":"delete","kind":"sparse_point","entity_id":"b"}])
        finally:other.close()
        second=self.store.read_map(self.mid,cursor=page["next_cursor"],limit=1)
        self.assertEqual(second["map"]["revision"],2)
        self.assertEqual(second["entities"][0]["entity_id"],"b")
        self.assertEqual(len(self.store.read_map(self.mid)["entities"]),1)
    def test_migration_backfills_existing_history_and_tombstones(self):
        self.apply(0,"a");self.apply(1,"a",delete=True);self.apply(2,"b",kind="keyframe")
        for name in ["entity_heads_insert","entity_heads_delete","entity_heads_update"]:
            self.store._db.execute("DROP TRIGGER "+name)
        self.store._db.execute("DROP TABLE entity_heads")
        self.store.close();self.store=VisionStore(self.tmp.name)
        self.assertEqual(len(self.store.read_map(self.mid)["entities"]),1)
        self.assertEqual(self.store._db.execute("SELECT COUNT(*) FROM entity_heads").fetchone()[0],2)
        self.apply(3,"a")
        self.assertEqual(len(self.store.read_map(self.mid)["entities"]),2)
    def test_maintenance_delete_and_rollback_repair_heads_atomically(self):
        self.apply(0,"a");self.apply(1,"a")
        with self.store._transaction():
            self.store._db.execute("DELETE FROM entity_versions WHERE map_id=? AND revision=2",(self.mid,))
        self.assertEqual(self.store.read_map(self.mid)["entities"][0]["revision"],1)
        try:
            with self.store._transaction():
                self.store._db.execute("DELETE FROM entity_versions WHERE map_id=?",(self.mid,))
                raise ValueError("rollback")
        except ValueError:pass
        self.assertEqual(len(self.store.read_map(self.mid)["entities"]),1)
    def test_current_kind_query_uses_heads_without_history_aggregation(self):
        self.apply(0,"a");self.apply(1,"b",kind="keyframe")
        statements=[]
        self.store._db.set_trace_callback(statements.append)
        try:page=self.store.read_map(self.mid,kinds=["keyframe"])
        finally:self.store._db.set_trace_callback(None)
        self.assertEqual(page["entities"][0]["entity_id"],"b")
        self.assertTrue(any("FROM entity_heads" in x for x in statements))
        self.assertFalse(any("GROUP BY" in x for x in statements))

if __name__=="__main__":unittest.main()
