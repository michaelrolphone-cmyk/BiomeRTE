"""Synthetic, deterministic tests of bounded adaptive radio place inference."""
import copy
import math
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from radio_map import RadioMap, source_identity, validate_config


def transform(x=0.0, y=0.0, z=0.0, quarter_turn=False):
    return ([[0,-1,0,x],[1,0,0,y],[0,0,1,z],[0,0,0,1]] if quarter_turn
            else [[1,0,0,x],[0,1,0,y],[0,0,1,z],[0,0,0,1]])


class Clock:
    def __init__(self):
        self.mono = 100.0
        self.utc = 1700000000.0

    def __call__(self):
        return self.utc

    def advance(self, amount=1.0):
        self.mono += amount
        self.utc += amount


class RadioMapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.sequence = 0
        self.engine = RadioMap(Path(self.directory.name)/"radio.sqlite3", clock=self.clock)

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def reopen(self, config=None):
        self.engine.close()
        self.engine = RadioMap(Path(self.directory.name)/"radio.sqlite3",
                               config=config, clock=self.clock)

    def fingerprint(self, values=(-45,-65,-75,-85), *, boot="boot-one", receiver="camera",
                    advance=True, radio="wifi"):
        if advance:
            self.clock.advance()
        self.sequence += 1
        rows = []
        for index, rssi in enumerate(values):
            row = {"radio":radio, "address":"02:00:00:00:00:%02x" % (index+1),
                   "rssi_dbm":rssi, "rssi_filtered_dbm":-1,
                   "observed_at_s":self.clock.mono, "time_uncertainty_s":0.01,
                   "observed_uptime_ms":self.sequence*1000, "sample_count":self.sequence,
                   "distance_m":999999, "distance_method":"rssi_assumed"}
            if radio == "ble":
                row["address_type"] = 1
            rows.append(row)
        return {"receiver_id":receiver, "boot_id":boot, "observations":rows}

    def visual(self):
        return {"valid":True, "source":"visual", "independently_validated":True,
                "map_id":"map", "calibration_id":"cal", "timestamp_s":self.clock.mono,
                "source_frame_id":"visual-%d" % self.sequence}

    def anchor(self, identifier="0", relative=None, source=None):
        return {"keyframe_id":str(identifier),
                "source_frame_id":source or "capture-"+str(identifier),
                "T_keyframe_camera":relative or transform()}

    def keys(self, positions=(0,1,2)):
        return {str(i):{"T_map_camera":transform(x), "position":[x,0,0],
                       "units":"arbitrary", "source_frame_id":"capture-"+str(i)}
                for i,x in enumerate(positions)}

    def train(self, identifier="0", values=(-45,-65,-75,-85), **kwargs):
        fp = kwargs.pop("fingerprint", None) or self.fingerprint(values)
        anchor = kwargs.pop("anchor", None) or self.anchor(identifier)
        pose = kwargs.pop("visual_pose", None) or self.visual()
        result = self.engine.train("map", "cal", fp, pose, anchor,
                                   now_s=self.clock.mono, now_utc_s=self.clock.utc, **kwargs)
        return result, fp

    def teach(self, identifier, values, count=4):
        for _ in range(count):
            result,_ = self.train(identifier, values)
            self.assertTrue(result["accepted"], result)

    def estimate(self, fp, keys=None):
        return self.engine.estimate("map", "cal", fp, keys or self.keys(),
                                    now_s=self.clock.mono, now_utc_s=self.clock.utc)

    def entry(self, identifier="0", keys=None):
        entries = self.engine.read_map("map","cal",keys or self.keys())["entries"]
        return next(e for e in entries if e["keyframe_id"]==str(identifier))

    def test_out_of_sample_spatial_interpolation_is_coarse_not_navigation(self):
        self.teach("0", (-40,-80,-55,-75))
        self.teach("1", (-60,-60,-55,-75))
        self.teach("2", (-80,-40,-55,-75))
        query = self.fingerprint((-46,-74,-55,-75))
        estimate = self.estimate(query)
        self.assertEqual(estimate["state"], "coarse_match")
        self.assertAlmostEqual(estimate["position"][0], .3, delta=.15)
        self.assertEqual(estimate["candidate_keyframe_ids"][0], "0")
        self.assertFalse(estimate["valid_visual_pose"])
        self.assertFalse(estimate["heading_available"])
        self.assertFalse(estimate["navigation_usable"])
        self.assertFalse(estimate["metric_position"])
        self.assertEqual(estimate["units"], "arbitrary")
        self.assertEqual(estimate["confidence"]["kind"], "heuristic_not_probability")

    def test_identical_radio_at_distant_places_stays_multimodal(self):
        self.teach("0", (-45,-65,-75,-85))
        self.teach("1", (-45,-65,-75,-85))
        result = self.estimate(self.fingerprint(), self.keys((0,100)))
        self.assertEqual(result["state"], "ambiguous")
        self.assertIsNone(result["position"])
        self.assertEqual(len(result["clusters"]), 2)
        self.assertTrue(result["disagreement"]["ambiguous"])
        self.assertNotIn([50,0,0], [c["position"] for c in result["clusters"]])

    def test_republication_and_http_sequence_cannot_retrain(self):
        accepted, fp = self.train()
        self.assertTrue(accepted["accepted"])
        self.clock.advance()
        repeated = copy.deepcopy(fp)
        repeated["sequence"] = 99999
        for row in repeated["observations"]:
            row["observed_at_s"] = self.clock.mono
        result,_ = self.train(fingerprint=repeated)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "replayed_observation")
        self.assertEqual(self.engine.status()["counts"]["samples"], 1)

    def test_deduplication_survives_reopen_and_model_clear(self):
        _,fp = self.train()
        self.reopen()
        result,_ = self.train(fingerprint=fp)
        self.assertFalse(result["accepted"])
        self.engine.clear("map")
        self.assertEqual(self.engine.status()["counts"]["samples"], 0)
        result,_ = self.train(fingerprint=fp)
        self.assertEqual(result["reason"], "replayed_observation")
        self.assertTrue(self.train()[0]["accepted"])

    def test_uint32_uptime_wrap_and_sample_counter_restart_are_real_new_events(self):
        fp = self.fingerprint()
        for row in fp["observations"]:
            row["observed_uptime_ms"] = 0xfffffff0
            row["sample_count"] = 50
        self.assertTrue(self.train(fingerprint=fp)[0]["accepted"])
        new = self.fingerprint()
        for row in new["observations"]:
            row["observed_uptime_ms"] = 32
            row["sample_count"] = 1
        self.assertTrue(self.train(fingerprint=new)[0]["accepted"])
        old = self.fingerprint()
        for row in old["observations"]:
            row["observed_uptime_ms"] = 0xfffffff0
            row["sample_count"] = 1000
        self.assertEqual(self.train(fingerprint=old)[0]["reason"], "replayed_observation")

    def test_reboot_is_separate_but_retired_boot_cannot_return(self):
        self.assertTrue(self.train()[0]["accepted"])
        self.assertTrue(self.train(fingerprint=self.fingerprint(boot="boot-two"))[0]["accepted"])
        result,_ = self.train(fingerprint=self.fingerprint(boot="boot-one"))
        self.assertEqual(result["reason"], "retired_boot")
        self.assertEqual(self.estimate(self.fingerprint(boot="boot-one"))["state"], "retired_boot")

    def test_stale_sparse_empty_and_incoherent_observations_are_unavailable(self):
        fp = self.fingerprint()
        self.clock.advance(16)
        self.assertEqual(self.estimate(fp)["state"], "no_fresh_radio")
        self.assertFalse(self.train(fingerprint=fp)[0]["accepted"])
        self.assertEqual(self.estimate(self.fingerprint(values=()))["state"], "no_fresh_radio")
        self.assertEqual(self.estimate(self.fingerprint(values=(-50,-60)))["state"], "insufficient_sources")
        fp = self.fingerprint()
        fp["observations"][0]["observed_at_s"] -= 2
        self.assertEqual(self.estimate(fp)["state"], "incoherent_fingerprint")

    def test_future_and_nan_data_are_not_learned(self):
        fp = self.fingerprint()
        for row in fp["observations"]:
            row["observed_at_s"] += 30
        self.assertFalse(self.train(fingerprint=fp)[0]["accepted"])
        fp = self.fingerprint()
        for row in fp["observations"]:
            row["rssi_dbm"] = float("nan")
        self.assertFalse(self.train(fingerprint=fp)[0]["accepted"])

    def test_invalid_and_radio_derived_pose_never_train(self):
        fp = self.fingerprint()
        for update in ({"valid":False}, {"source":"radio"},
                       {"independently_validated":False}):
            pose = dict(self.visual(), **update)
            result,_ = self.train(fingerprint=fp, visual_pose=pose)
            self.assertEqual(result["reason"], "independent_visual_pose_required")
        self.assertEqual(self.engine.status()["counts"]["samples"], 0)

    def test_time_uncertainty_and_pose_time_alignment_are_required(self):
        fp = self.fingerprint()
        pose = self.visual()
        pose["timestamp_s"] -= 0.6
        self.assertEqual(self.train(fingerprint=fp, visual_pose=pose)[0]["reason"],
                         "visual_radio_time_mismatch")
        fp = self.fingerprint()
        for row in fp["observations"]:
            row["time_uncertainty_s"] = 0.6
        self.assertEqual(self.train(fingerprint=fp)[0]["reason"], "visual_radio_time_mismatch")

    def test_map_calibration_receiver_and_rf_calibration_are_isolated(self):
        self.teach("0", (-45,-65,-75,-85))
        fp = self.fingerprint()
        bad = dict(self.visual(), map_id="different")
        self.assertEqual(self.train(fingerprint=fp, visual_pose=bad)[0]["reason"], "visual_context_mismatch")
        result = self.engine.estimate("other","cal",fp,self.keys(),now_s=self.clock.mono)
        self.assertEqual(result["state"], "no_model")
        result = self.engine.estimate("map","other-cal",fp,self.keys(),now_s=self.clock.mono)
        self.assertEqual(result["state"], "no_model")
        fp["receiver_id"] = "other-camera"
        self.assertEqual(self.estimate(fp)["state"], "no_model")
        fp["receiver_id"] = "camera"
        fp["receiver_calibration_id"] = "different-front-end"
        self.assertEqual(self.estimate(fp)["state"], "no_model")

    def test_source_identity_uses_bssid_and_ble_type_or_beacon(self):
        a = {"radio":"wifi", "address":"AA:BB:CC:DD:EE:01","name":"same network"}
        b = dict(a, address="aa:bb:cc:dd:ee:02")
        self.assertNotEqual(source_identity(a), source_identity(b))
        a = {"radio":"ble","address":"aa:bb:cc:dd:ee:01","address_type":0}
        self.assertNotEqual(source_identity(a), source_identity(dict(a,address_type=1)))
        beacon={"uuid":"12345678-1234-1234-1234-123456789abc","major":1,"minor":2}
        a["ibeacon"]=beacon
        self.assertEqual(source_identity(a), source_identity(dict(a,address="aa:bb:cc:dd:ee:02",address_type=1)))
        self.assertNotEqual(source_identity(a), source_identity(dict(a,ibeacon=dict(beacon,minor=3))))

    def test_assumed_ranges_and_filtered_history_do_not_change_inference(self):
        self.teach("0", (-45,-65,-75,-85))
        fp = self.fingerprint()
        first = self.estimate(fp)
        for row in fp["observations"]:
            row["distance_m"] = 0.001
            row["rssi_filtered_dbm"] = -126
            row["tx_power_dbm"] = 127
        second = self.estimate(fp)
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertFalse(self.engine.status()["radio_range_used"])

    def test_loop_closure_reprojects_relative_anchor_and_culling_excludes(self):
        anchor=self.anchor(relative=transform(.25))
        self.assertTrue(self.train(anchor=anchor)[0]["accepted"])
        before = self.entry(keys=self.keys((0,)))
        self.assertAlmostEqual(before["position"][0], .25)
        keys=self.keys((10,))
        keys["0"]["T_map_camera"] = transform(10,quarter_turn=True)
        after=self.entry(keys=keys)
        self.assertAlmostEqual(after["position"][0],10)
        self.assertAlmostEqual(after["position"][1],.25)
        self.assertEqual(self.engine.read_map("map","cal",{})["entries"],[])
        self.assertEqual(self.engine.estimate("map","cal",self.fingerprint(),{},
                         now_s=self.clock.mono)["unresolved_anchor_count"],1)

    def test_anchor_incarnation_is_not_numeric_keyframe_id(self):
        self.assertTrue(self.train()[0]["accepted"])
        keys=self.keys()
        keys["0"]["source_frame_id"]="different-capture"
        self.assertEqual(self.engine.read_map("map","cal",keys)["entries"],[])
        del keys["0"]["source_frame_id"]
        self.assertEqual(self.engine.read_map("map","cal",keys)["entries"],[])
        result,_ = self.train(anchor=self.anchor(source="different-capture"), values=(-80,-50,-60,-70))
        self.assertTrue(result["accepted"])
        keys["0"]["source_frame_id"]="different-capture"
        entry=self.entry(keys=keys)
        self.assertEqual(entry["sample_count"],1)
        self.assertEqual(entry["anchor_source_frame_id"],"different-capture")
        self.assertEqual(entry["sources"][0]["mean_rssi_dbm"],-80)

    def test_anchor_capture_identity_and_rigid_transform_are_required(self):
        a=self.anchor()
        del a["source_frame_id"]
        self.assertFalse(self.train(anchor=a)[0]["accepted"])
        a=self.anchor()
        a["T_keyframe_camera"][0][0]=2
        self.assertFalse(self.train(anchor=a)[0]["accepted"])

    def test_repeated_measurements_update_distribution_and_variability(self):
        for rssi in (-50,-70,-40,-80):
            self.assertTrue(self.train(values=(rssi,-65,-75,-85))[0]["accepted"])
        source=self.entry()["sources"][0]
        self.assertEqual(source["sample_count"],4)
        self.assertGreater(source["variance_db2"],200)
        self.assertGreater(source["effective_sample_count"],3.9)
        self.assertEqual(source["transmitter_motion"],"not_inferred")

    def test_stable_spatial_variation_is_not_global_instability(self):
        self.teach("0",(-40,-80,-55,-75))
        self.teach("1",(-75,-45,-55,-75))
        first=self.entry("0")["sources"][0]
        second=self.entry("1")["sources"][0]
        self.assertAlmostEqual(first["variance_db2"],0)
        self.assertAlmostEqual(second["variance_db2"],0)
        self.assertGreater(first["reliability"],.6)
        self.assertGreater(second["reliability"],.6)
        self.assertEqual(self.estimate(self.fingerprint((-40,-80,-55,-75)),
                                      self.keys((0,100)))["candidate_keyframe_ids"][0],"0")

    def test_volatile_source_is_downweighted_within_location(self):
        for fourth in (-30,-100,-30,-100,-30,-100):
            self.assertTrue(self.train("0",(-40,-75,-65,fourth))[0]["accepted"])
        self.teach("1",(-65,-50,-65,-35),6)
        sources=self.entry("0")["sources"]
        self.assertGreater(sources[3]["variance_db2"],1000)
        self.assertLess(sources[3]["reliability"],sources[0]["reliability"]/10)
        query=self.fingerprint((-40,-75,-65,-30))
        self.assertEqual(self.estimate(query,self.keys((0,100)))["candidate_keyframe_ids"][0],"0")

    def test_common_shift_is_exposed_and_penalized_not_independent_confirmation(self):
        self.teach("0",(-45,-65,-75,-85))
        clean=self.estimate(self.fingerprint((-45,-65,-75,-85)))
        shifted=self.estimate(self.fingerprint((-41,-61,-71,-81)))
        self.assertAlmostEqual(shifted["candidates"][0]["common_offset_db"],4)
        self.assertLess(shifted["confidence"]["score"],clean["confidence"]["score"])
        self.assertFalse(shifted["confidence"]["independent_sources_assumed"])

    def test_temporal_decay_adapts_to_changed_source_without_erasing_history(self):
        self.reopen({"decay_half_life_s":10})
        self.teach("0",(-40,-65,-75,-85))
        self.clock.advance(60)
        self.teach("0",(-80,-65,-75,-85))
        source=self.entry()["sources"][0]
        self.assertEqual(source["sample_count"],8)
        self.assertLess(source["mean_rssi_dbm"],-78)
        self.assertLess(source["effective_weight"],4.1)

    def test_obsolete_evidence_decays_and_true_retention_removes_contributions(self):
        self.reopen({"retention_s":60,"dedup_retention_s":300,"decay_half_life_s":10})
        self.teach("0",(-45,-65,-75,-85))
        fresh=self.estimate(self.fingerprint())
        self.clock.advance(40)
        old=self.estimate(self.fingerprint())
        self.assertLess(old["confidence"]["score"],fresh["confidence"]["score"]/4)
        self.assertLess(self.entry()["effective_weight"],.5)
        self.clock.advance(30)
        self.engine.prune()
        self.assertEqual(self.engine.status()["counts"]["samples"],0)
        self.assertEqual(self.engine.read_map("map","cal",self.keys())["entries"],[])

    def test_bounded_history_preserves_quiet_locations_under_hot_location_traffic(self):
        self.reopen({"max_samples":8,"max_samples_per_anchor":6,"max_anchors":4,
                     "max_db_bytes":524288})
        for anchor in range(4):
            self.teach(str(anchor),(-40-anchor*5,-60,-70,-80),3)
        for _ in range(50):
            self.assertTrue(self.train("3",(-55,-60,-70,-80))[0]["accepted"])
        status=self.engine.status()
        self.assertLessEqual(status["counts"]["samples"],8)
        self.assertEqual(status["counts"]["anchors"],4)
        entries=self.engine.read_map("map","cal",self.keys((0,1,2,3)))["entries"]
        self.assertEqual({e["keyframe_id"] for e in entries},{"0","1","2","3"})
        self.assertLessEqual(status["storage"]["bytes"],524288)
        self.assertFalse(Path(str(self.engine.path)+"-wal").exists())

    def test_temporal_coverage_keeps_old_and_recent_independent_samples(self):
        self.reopen({"max_samples_per_anchor":6})
        for _ in range(20):
            self.train()
            self.clock.advance(30)
        rows=self.engine._db.execute("SELECT observed_utc FROM samples ORDER BY observed_utc").fetchall()
        self.assertEqual(len(rows),6)
        self.assertLess(rows[0][0],self.clock.utc-500)
        self.assertGreater(rows[-1][0],self.clock.utc-40)

    def test_watermark_capacity_fails_closed_instead_of_forgetting_replays(self):
        self.reopen({"max_watermarks":4})
        self.assertTrue(self.train()[0]["accepted"])
        fp=self.fingerprint()
        fp["observations"][0]["address"]="02:00:00:00:00:10"
        self.assertEqual(self.train(fingerprint=fp)[0]["reason"],"dedup_capacity")
        self.assertEqual(self.engine.status()["counts"]["watermarks"],4)
        self.assertEqual(self.engine.status()["counts"]["samples"],1)

    def test_map_read_has_per_source_statistics_and_revision_guarded_pages(self):
        for i in range(3):
            self.teach(str(i),(-40-i*10,-60,-70,-80))
        source=source_identity(self.fingerprint()["observations"][0])
        full=self.engine.read_map("map","cal",self.keys())
        filtered=self.engine.read_map("map","cal",self.keys(),source_id=source)
        self.assertEqual(len(filtered["entries"]),3)
        self.assertTrue(all(len(e["sources"])==1 for e in filtered["entries"]))
        first=self.engine.read_map("map","cal",self.keys(),limit=1)
        self.assertIsNotNone(first["next_cursor"])
        self.clock.advance(60)
        second=self.engine.read_map("map","cal",self.keys(),limit=1,cursor=first["next_cursor"])
        self.assertEqual(second["evaluated_at_utc_s"],first["evaluated_at_utc_s"])
        self.assertEqual(second["entries"][0]["sources"],full["entries"][1]["sources"])
        changed=self.keys()
        changed["1"]["T_map_camera"]=transform(50)
        with self.assertRaisesRegex(ValueError,"cursor"):
            self.engine.read_map("map","cal",changed,limit=1,cursor=first["next_cursor"])
        self.train()
        with self.assertRaisesRegex(ValueError,"cursor"):
            self.engine.read_map("map","cal",self.keys(),limit=1,cursor=first["next_cursor"])

    def test_config_unknown_invalid_and_inconsistent_bounds_are_rejected(self):
        for config in ({"enabled":True},{"max_samples":3},{"decay_half_life_s":0},
                       {"min_similarity":2},{"max_anchors":600},
                       {"retention_s":1000,"dedup_retention_s":500}):
            with self.assertRaises(ValueError):
                validate_config(config)


    def test_same_keyframe_separated_regions_and_pagination_do_not_pool_modes(self):
        for location in (0,100):
            for _ in range(4):
                self.assertTrue(self.train(anchor=self.anchor(relative=transform(location)))[0]["accepted"])
        result=self.estimate(self.fingerprint(),self.keys((0,)))
        self.assertEqual(result["state"],"ambiguous")
        self.assertIsNone(result["position"])
        self.assertEqual(result["candidate_keyframe_ids"],["0"])
        self.assertEqual({round(c["position"][0]) for c in result["clusters"]},{0,100})
        full=self.engine.read_map("map","cal",self.keys((0,)))
        self.assertEqual(full["total_entries"],2)
        self.assertEqual(len({e["region_id"] for e in full["entries"]}),2)
        self.assertEqual(full["spatial_bin_size_map_units"],.5)
        first=self.engine.read_map("map","cal",self.keys((0,)),limit=1)
        self.clock.advance(10)
        second=self.engine.read_map("map","cal",self.keys((0,)),limit=1,cursor=first["next_cursor"])
        self.assertEqual(second["entries"][0]["region_id"],full["entries"][1]["region_id"])
        self.assertEqual(second["entries"][0]["sources"],full["entries"][1]["sources"])

    def test_burst_repeats_preserve_variance_without_multiplying_pose_support(self):
        for i in range(12):
            self.clock.advance(.02)
            fp=self.fingerprint((-45+i%2*4,-65,-75,-85),advance=False)
            pose=dict(self.visual(),source_frame_id="one-independent-visual-frame")
            self.assertTrue(self.train(fingerprint=fp,visual_pose=pose)[0]["accepted"])
        entry=self.entry()
        self.assertEqual(entry["sample_count"],12)
        self.assertEqual(entry["visual_frame_count"],1)
        self.assertEqual(entry["sampling_epoch_count"],1)
        self.assertLessEqual(entry["effective_weight"],1.000001)
        self.assertGreater(entry["sources"][0]["variance_db2"],3)
        self.assertEqual(entry["sources"][0]["effective_sample_count"],1)
        result=self.estimate(self.fingerprint())
        self.assertEqual(result["state"],"insufficient_model_support")
        self.assertIsNone(result["position"])
        self.assertEqual(result["candidate_keyframe_ids"],["0"])

    def test_unreliable_or_off_manifold_radio_abstains(self):
        for value in (-30,-110,-30,-110,-30,-110):
            self.assertTrue(self.train(values=(value,value,value,value))[0]["accepted"])
        result=self.estimate(self.fingerprint((-70,-70,-70,-70)))
        self.assertEqual(result["state"],"insufficient_model_support")
        self.assertIsNone(result["position"])
        self.assertEqual(result["candidates"][0]["maturity"]["reliable_source_count"],0)
        self.engine.clear()
        self.teach("0",(-45,-65,-75,-85))
        result=self.estimate(self.fingerprint((-5,-5,-5,-5)))
        self.assertEqual(result["state"],"insufficient_overlap")
        self.assertIsNone(result["position"])

    def test_region_diversity_and_singleton_regions_obey_existing_storage_caps(self):
        self.reopen({"max_samples":8,"max_samples_per_anchor":8,"max_anchors":1})
        for location in (0,10):
            for _ in range(3):
                self.assertTrue(self.train(anchor=self.anchor(relative=transform(location)))[0]["accepted"])
        for _ in range(20):
            self.train(anchor=self.anchor(relative=transform(10)))
        entries=self.engine.read_map("map","cal",self.keys((0,)))["entries"]
        self.assertEqual({round(e["position"][0]) for e in entries},{0,10})
        self.assertTrue(all(e["visual_frame_count"]>=3 for e in entries))
        for location in range(20,60,2):
            self.train(anchor=self.anchor(relative=transform(location)))
        status=self.engine.status()
        self.assertLessEqual(status["counts"]["samples"],8)
        self.assertLessEqual(status["counts"]["regions"],8)
        self.assertEqual(status["counts"]["anchors"],1)

    def test_decay_survives_monotonic_restart_and_boot_history_fails_closed(self):
        self.teach("0",(-45,-65,-75,-85))
        before=self.entry()["effective_weight"]
        self.clock.advance(86400)
        self.clock.mono=0
        self.reopen({"max_boots_per_receiver":2})
        self.assertAlmostEqual(self.entry()["effective_weight"],before/2,places=6)
        self.assertEqual(self.estimate(self.fingerprint())["candidate_keyframe_ids"],["0"])
        self.assertTrue(self.train(fingerprint=self.fingerprint(boot="boot-two"))[0]["accepted"])
        self.assertEqual(self.train(fingerprint=self.fingerprint(boot="boot-three"))[0]["reason"],
                         "boot_history_capacity")
        self.assertEqual(self.engine.status()["counts"]["boots"],2)
        self.assertEqual(self.train(fingerprint=self.fingerprint(boot="boot-one"))[0]["reason"],
                         "retired_boot")

    def test_sqlite_main_file_ceiling_rolls_back_watermarks_and_reclaims_space(self):
        self.reopen({"max_db_bytes":262144})
        failed=None
        for _ in range(40):
            fp=self.fingerprint(tuple(-40-i%40 for i in range(96)))
            result,_=self.train(fingerprint=fp)
            if not result["accepted"]:
                self.assertEqual(result["reason"],"storage_capacity")
                failed=fp
                break
        self.assertIsNotNone(failed)
        self.assertLessEqual(self.engine.path.stat().st_size,262144)
        self.engine.clear("map")
        self.assertTrue(self.train(fingerprint=failed)[0]["accepted"])
        self.assertLessEqual(self.engine.path.stat().st_size,262144)

    def test_ambiguity_is_measured_before_candidate_display_budget(self):
        positions=[i*.01 for i in range(13)]+[100]
        for index in range(14):
            self.teach(str(index),(-45,-65,-75,-85),3)
        result=self.estimate(self.fingerprint(),self.keys(positions))
        self.assertEqual(len(result["candidates"]),12)
        self.assertEqual(result["candidate_count"],14)
        self.assertTrue(result["candidates_truncated"])
        self.assertIn("13",result["candidate_keyframe_ids"][:6])
        self.assertIn("13",[row["keyframe_id"] for row in result["candidates"][:6]])
        self.assertEqual(result["state"],"ambiguous")
        self.assertIsNone(result["position"])
        self.assertEqual(result["disagreement"]["separated_mode_count"],2)
        self.assertTrue(any(c["position"][0]>90 for c in result["clusters"]))

    def test_cursor_geometry_is_not_overwritten_by_another_receiver_incarnation(self):
        self.teach("0",(-45,-65,-75,-85))
        self.teach("1",(-45,-65,-75,-85))
        for index in range(8):
            fp=self.fingerprint(receiver="old-receiver-"+str(index))
            self.assertTrue(self.train(fingerprint=fp,
                anchor=self.anchor(source="old-capture"))[0]["accepted"])
        first=self.engine.read_map("map","cal",self.keys(),limit=1)
        changed=self.keys()
        changed["0"]["T_map_camera"]=transform(50)
        with self.assertRaisesRegex(ValueError,"cursor"):
            self.engine.read_map("map","cal",changed,limit=1,cursor=first["next_cursor"])

    def test_legacy_schema_migrates_spatial_cells(self):
        path=Path(self.directory.name)/"legacy.sqlite3"
        c=sqlite3.connect(str(path))
        c.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
            INSERT INTO meta VALUES('revision',5);
            CREATE TABLE contexts(id TEXT PRIMARY KEY,map_id TEXT NOT NULL,calibration_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,receiver_calibration_id TEXT NOT NULL,last_seen REAL NOT NULL);
            CREATE TABLE samples(id INTEGER PRIMARY KEY AUTOINCREMENT,context_id TEXT NOT NULL,
                anchor_id TEXT NOT NULL,anchor_source_frame_id TEXT NOT NULL,frame_id TEXT NOT NULL,
                observed_utc REAL NOT NULL,relative_position TEXT NOT NULL,sources TEXT NOT NULL);
            PRAGMA user_version=1;
        """)
        c.execute("INSERT INTO contexts VALUES('ctx','map','cal','camera','rssi-v1',?)",(self.clock.utc,))
        sources={source_identity(row):{"radio":"wifi","address":row["address"],
            "identity_stability":"bssid_not_a_stationarity_claim","rssi_dbm":row["rssi_dbm"],
            "observed_utc":self.clock.utc} for row in self.fingerprint()["observations"]}
        c.execute("INSERT INTO samples(context_id,anchor_id,anchor_source_frame_id,frame_id,"
            "observed_utc,relative_position,sources) VALUES(?,?,?,?,?,?,?)",
            ("ctx","0","capture-0","old-frame",self.clock.utc,json.dumps([.75,0,0]),json.dumps(sources)))
        c.commit();c.close()
        engine=RadioMap(path,clock=self.clock)
        try:
            entry=engine.read_map("map","cal",self.keys())["entries"][0]
            self.assertEqual(engine.status()["schema_version"],2)
            self.assertEqual(entry["spatial_cell"],[1,0,0])
            self.assertEqual(entry["position"],[.75,0,0])
        finally:
            engine.close()


if __name__=="__main__":
    unittest.main()
