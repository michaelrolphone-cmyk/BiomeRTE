import sqlite3
import tempfile
import unittest
from pathlib import Path
from multisensor_mapping import SonarObstacleMap

class ObstacleSummaryTests(unittest.TestCase):
    def test_legacy_backfill_external_writes_and_no_per_frame_ray_scan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"sonar.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE rays(id TEXT PRIMARY KEY,map_id TEXT NOT NULL,data TEXT NOT NULL,resolved INTEGER NOT NULL,metric_resolved INTEGER NOT NULL,observed_s REAL NOT NULL)")
            db.executemany("INSERT INTO rays VALUES(?,?,?,?,?,?)",
                           [("a","map","{}",1,0,1.),("b","map","{}",0,1,2.)])
            db.commit(); db.close()
            model = SonarObstacleMap(path)
            self.addCleanup(model.close)
            value = model.summary("map")
            self.assertEqual((value["ray_count"],value["posed_observations"],value["metric_observations"]),(2,1,1))
            external = sqlite3.connect(path)
            external.execute("UPDATE rays SET resolved=1,metric_resolved=1 WHERE id='b'")
            external.execute("INSERT OR IGNORE INTO rays VALUES('a','map','{}',1,0,1.)")
            external.commit()
            def authorize(action, table, *unused):
                return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=="rays" else sqlite3.SQLITE_OK
            model.db.set_authorizer(authorize)
            value=model.summary("map")
            self.assertEqual((value["ray_count"],value["posed_observations"],value["metric_observations"]),(2,2,1))
            model.db.set_authorizer(None)
            external.execute("UPDATE rays SET map_id='other' WHERE id='b'")
            external.execute("DELETE FROM rays WHERE id='a'")
            external.commit();external.close()
            self.assertEqual(model.summary("map")["ray_count"],0)
            self.assertEqual(model.summary("other")["ray_count"],1)
            self.assertEqual(model.db.execute("SELECT count(*) FROM rays").fetchone()[0],1)
