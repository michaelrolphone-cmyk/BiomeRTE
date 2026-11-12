"""Bounded rolling measurements of the live frame-processing budget."""
import math
import os
import time
from collections import deque

class ProcessingPerformance:
    def __init__(self, fps, capacity=120):
        self.period_ms = 1000.0 / max(float(fps), 0.01)
        self.samples = deque(maxlen=capacity)
        self.summary = {}
        self.previous_capture = None
        self.last_write = 0.0

    def record(self, total_ms, stages, captured, skipped, valid, now=None):
        now = time.monotonic() if now is None else now
        stamp = captured.metadata.get("measurement_monotonic_s", captured.decoded_monotonic)
        gap_ms = None if self.previous_capture is None else (stamp-self.previous_capture)*1000
        self.previous_capture = stamp
        self.samples.append(dict(at=now, total_ms=total_ms, stages=dict(stages),
                                 skipped=skipped, valid=valid, gap_ms=gap_ms))
        rows = list(self.samples)
        def percentile(values, q):
            values = sorted(v for v in values if v is not None)
            return values[max(0, math.ceil(len(values)*q)-1)] if values else None
        p95 = percentile([r["total_ms"] for r in rows], .95)
        elapsed = now-rows[0]["at"]
        self.summary = {
            "process_id": os.getpid(), "window_frames": len(rows), "expected_frame_period_ms": self.period_ms,
            "processing_ms_p50": percentile([r["total_ms"] for r in rows], .50),
            "processing_ms_p95": p95,
            "frame_budget_headroom_fraction_p95": 1-p95/self.period_ms,
            "processed_fps_window": (len(rows)-1)/elapsed if elapsed > 0 else None,
            "skipped_camera_frames_window": skipped-rows[0]["skipped"],
            "invalid_localization_frames_window": sum(not r["valid"] for r in rows),
            "processed_capture_gap_ms_p95": percentile([r["gap_ms"] for r in rows], .95),
            "decode_to_processed_age_ms": max(0, (now-captured.decoded_monotonic)*1000),
            "stage_ms_last": dict(stages),
            "stage_ms_p95": {k: percentile([r["stages"].get(k) for r in rows], .95)
                             for k in stages},
            "sample_monotonic_s": now,
            "scope": "frame_pipeline_budget_not_whole_machine_cpu_or_gpu_utilization",
        }
        return self.summary
