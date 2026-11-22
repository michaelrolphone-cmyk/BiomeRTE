"""Recognize an archived surveyed rover view off the camera processing thread."""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from survey_grid_geometry import corners, partial_board, camera_pose

LOG = logging.getLogger(__name__)


class SurveyGridMatcher:
    def __init__(self, folder, world_id, calibration_id):
        self.data = json.loads((folder / "grid.json").read_text())
        if (self.data["world_id"], self.data["calibration_id"]) != (world_id, calibration_id):
            raise ValueError("survey grid belongs to another world or calibration")
        reference = self.data["observations"][self.data["reference_record"]]
        img = cv2.imread(str(folder / reference["file"]), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError("survey reference image unavailable")
        self.shape = img.shape
        self.image = img
        self.cal = self.data["intrinsics"]
        self.detector = cv2.ORB_create(nfeatures=1100, fastThreshold=8)
        mask = np.full(img.shape, 255, np.uint8)
        x,y,w,h = self.data["roi"]
        mask[y:y+h,x:x+w] = 0
        self.keys, self.desc = self.detector.detectAndCompute(self._contrast(img), mask)
        self.reference = np.asarray(self.data["reference_corners_px"], np.float32).reshape(6,9,2)
        if self.desc is None or len(self.desc) < 12:
            raise ValueError("insufficient scene descriptors in reference")

    @staticmethod
    def _contrast(gray):
        return cv2.createCLAHE(clipLimit=2.,tileGridSize=(8,8)).apply(gray)

    def match(self, gray):
        if gray.shape != self.shape:
            return {"status":"wrong_resolution"}
        keys, descriptors = self.detector.detectAndCompute(self._contrast(gray), None)
        if descriptors is None or len(descriptors) < 12:
            return {"status":"no_scene_match"}
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(self.desc, descriptors, k=2)
        matches = sorted((a for pair in pairs if len(pair)==2 for a,b in [pair] if a.distance < .76*b.distance),
                         key=lambda m:m.distance)
        unique = {}
        for m in matches:
            unique.setdefault(m.trainIdx, m)
        matches = list(unique.values())
        if len(matches) < 12:
            return {"status":"no_scene_match"}
        a = np.float32([self.keys[m.queryIdx].pt for m in matches])
        b = np.float32([keys[m.trainIdx].pt for m in matches])
        H, inliers = cv2.findHomography(a,b,cv2.RANSAC,3.)
        if H is None or inliers is None:
            return {"status":"no_scene_match"}
        good = inliers.ravel().astype(bool)
        coverage = cv2.contourArea(cv2.convexHull(a[good])) / gray.size
        if good.sum() < 12 or good.mean() < .45 or coverage < .055:
            return {"status":"insufficient_scene_geometry"}
        expected = cv2.perspectiveTransform(self.reference, H)
        if not np.isfinite(expected).all():
            return {"status":"invalid_projection"}
        lo = np.maximum(0, np.floor(expected.min(axis=(0,1))-32)).astype(int)
        hi = np.minimum(gray.shape[::-1], np.ceil(expected.max(axis=(0,1))+32)).astype(int)
        if np.any(hi-lo<22):
            return {"status":"grid_outside_view"}
        observed = corners(gray, [*lo,*(hi-lo)])
        mode = "complete_board"
        if observed is None:
            partial = partial_board(self.image, self.reference, gray, expected)
            if partial is None:
                return {"status":"scene_recognized_grid_unresolved","scene_inliers":int(good.sum())}
            ids, ordered = partial
            mode = "verified_partial_board"
        else:
            alternatives = [observed[::u,::v] for u in [1,-1] for v in [1,-1]]
            ordered = min(alternatives,key=lambda p:np.mean((p-expected)**2)).reshape(-1,2)
            ids = np.arange(54)
        source = expected.reshape(-1,2)[ids]
        residual = float(np.sqrt(np.mean(np.sum((ordered-source)**2,axis=1))))
        spacing = np.median(np.linalg.norm(np.diff(expected,axis=1),axis=2))
        if residual > max(3., .6*spacing):
            return {"status":"grid_identity_unresolved"}
        poses = camera_pose(self.data, ordered, ids, self.cal)
        return {"status":"survey_grid_matched","scene_inliers":int(good.sum()),
                "reference_transfer_rms_px":residual,"landmark_count":len(ids),"method":mode,
                "landmark_indices":ids.tolist(),"pose_candidates":poses,"pose_status":"planar_candidates",
                "world_id":self.data["world_id"],"raw_corners_px":ordered.round(3).tolist(),
                "reference_map_id":self.data["reference_map_id"]}


class AsyncSurveyGrid:
    def __init__(self, matcher):
        self.matcher = matcher
        self.pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix="survey-grid")
        self.future = None
        self.last = 0.
        self.lock = threading.Lock()
        self.latest = {}
        self.streak = 0

    def offer(self, frame_id, gray, map_id, calibration_id):
        now = time.monotonic()
        if (calibration_id != self.matcher.data["calibration_id"] or now-self.last < 1 or
                self.future is not None and not self.future.done()):
            return
        self.last = now
        self.future = self.pool.submit(self._inspect,frame_id,gray,map_id,now)

    def _inspect(self, frame_id, gray, map_id, captured):
        try:
            result = self.matcher.match(gray)
        except Exception:
            LOG.exception("Survey reference matching failed")
            result = {"status":"survey_match_failed"}
        with self.lock:
            same = self.latest.get("source_map_id")==map_id and captured-self.latest.get("sample_monotonic_s",-100)<4
            self.streak = (self.streak+1 if same else 1) if result["status"]=="survey_grid_matched" else 0
            self.latest = dict(result,frame_id=frame_id,source_map_id=map_id,sample_monotonic_s=captured,
                               consecutive_matches=self.streak)

    def status(self):
        with self.lock:
            return dict(self.latest)

    def close(self):
        self.pool.shutdown(wait=True)
