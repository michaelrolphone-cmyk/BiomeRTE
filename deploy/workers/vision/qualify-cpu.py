"""Qualify the real CPU detector, C++ optimizer and deployed SLAM adapter on fixtures."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/"providers/robot-vision")]

def qualify_optimizer(np):
    import g2o
    optimizer = g2o.SparseOptimizer()
    linear = g2o.LinearSolverEigenSE3()
    block = g2o.BlockSolverSE3(linear)
    algorithm = g2o.OptimizationAlgorithmLevenberg(block)
    optimizer.set_algorithm(algorithm)
    first = g2o.VertexSE3Expmap()
    first.set_id(0)
    first.set_estimate(g2o.SE3Quat(np.eye(3), np.zeros(3)))
    first.set_fixed(True)
    second = g2o.VertexSE3Expmap()
    second.set_id(1)
    second.set_estimate(g2o.SE3Quat(np.eye(3), np.array([2.0, 0.0, 0.0])))
    optimizer.add_vertex(first)
    optimizer.add_vertex(second)
    edge = g2o.EdgeSE3Expmap()
    edge.set_vertex(0, first)
    edge.set_vertex(1, second)
    edge.set_measurement(g2o.SE3Quat(np.eye(3), np.array([1.0, 0.0, 0.0])))
    edge.set_information(np.eye(6))
    optimizer.add_edge(edge)
    optimizer.initialize_optimization()
    optimizer.compute_active_errors()
    before = float(optimizer.active_chi2())
    iterations = int(optimizer.optimize(12))
    optimizer.compute_active_errors()
    after = float(optimizer.active_chi2())
    if before <= 0.5 or after >= 1e-8 or iterations < 1:
        raise RuntimeError("real g2o fixture did not converge")
    return {"engine":"g2o", "iterations":iterations, "initial_chi2":before, "final_chi2":after}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    args = parser.parse_args()
    if not args.model.is_file():
        parser.error("mounted detector weights are missing")
    import numpy as np
    import torch
    import cv2
    torch.set_num_threads(2)
    cv2.setNumThreads(2)
    from detector import ObjectDetector
    from slam_backend import SlamBackend
    calibration = {
        "calibration_id":"synthetic-qualification-only", "provenance":"synthetic-fixture",
        "validated":True, "width":640, "height":480, "fx":520.0, "fy":520.0,
        "cx":320.0, "cy":240.0, "distortion":[0.0,0.0,0.0,0.0,0.0],
    }
    with tempfile.TemporaryDirectory(prefix="biome-vision-cpu-") as directory:
        state = Path(directory)
        detector = ObjectDetector({"device":"cpu", "model":str(args.model.resolve()),
                                   "image_size":640, "confidence":0.25}, state).start()
        blank = np.zeros((480,640,3), dtype=np.uint8)
        detections = [detector.detect(blank, "synthetic-"+str(i), float(i)) for i in (1,2)]
        if any(r.get("availability") != "available" or r.get("device") != "cpu" for r in detections):
            raise RuntimeError("real detector fixture failed: "+str([r.get("reason") for r in detections]))
        if detector.execution_device() != "cpu":
            raise RuntimeError("detector executed on an unexpected device")
        backend = SlamBackend({"calibration":calibration, "fps":5.0, "num_features":600,
                               "pyslam_root":"/opt/vision/vendor/pyslam"}, state)
        try:
            started = backend.start("synthetic-qualification-only")
            optimizer = qualify_optimizer(np)
            texture = np.random.default_rng(731).integers(0,256,(480,640,3),dtype=np.uint8)
            tracked = [backend.track(np.roll(texture, i*2, axis=1), "synthetic-"+str(i), i/5.0)
                       for i in range(1,4)]
            status = backend.status()
            if status.get("state") in ("error","not_started","awaiting_calibration"):
                raise RuntimeError("actual SLAM adapter did not process fixture frames")
            print(json.dumps({
                "qualified":"linux-cpu-dependencies-and-fixture-execution",
                "detector":{"device":detector.execution_device(),"frames":len(detections),
                            "model_sha256":detections[-1]["model_sha256"]},
                "optimizer":optimizer,
                "slam":{"frames":len(tracked),"state":status.get("state"),
                        "pyslam_commit":"a4e6a31f211ac6b2f9ecf4ab628734f6194caa15"},
                "live_camera_used":False,"native_mps_configuration_changed":False,
                "checkpoint_relocalization_equivalence_qualified":False,
            },sort_keys=True))
        finally:
            backend.close()

if __name__ == "__main__":
    main()
