"""Immutable pySLAM capture plus FIFO database-save barrier.

The engine and its native workers remain live while file encoding and database
serialization finish. Exactly one checkpoint may be outstanding per backend.
"""
import copy
import importlib
import json
import os
from pathlib import Path
import pickle


def capture(slam, target):
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Checkpoint target must be empty")
    target.mkdir(parents=True, exist_ok=True)
    if slam.volumetric_integrator is not None:
        raise RuntimeError("Asynchronous volumetric checkpoints require a snapshot adapter")
    module = importlib.import_module(type(slam).__module__)
    serialize = module.SerializationJSON.serialize
    # Native to_json takes map and update RLocks while copying its graph.
    document = {
        "USE_CPP_CORE": module.Parameters.USE_CPP_CORE,
        "sensor_type": serialize(slam.sensor_type),
        "environment_type": serialize(slam.environment_type),
        "map": copy.deepcopy(slam.map.to_json()),
        "feature_tracker_config": serialize(slam.feature_tracker_config),
        "loop_detector_config": serialize(slam.loop_detector_config),
        "semantic_mapping_config": serialize(slam.semantic_mapping_config),
    }
    info = {k: copy.deepcopy(document[k]) for k in
            ("USE_CPP_CORE", "feature_tracker_config", "loop_detector_config",
             "semantic_mapping_config")}
    process = None
    images = None
    if slam.loop_closing is not None:
        loop = slam.loop_closing
        if not loop.headless:
            with loop.map_frame_id_to_img_lock:
                images = copy.deepcopy(loop.map_frame_id_to_img)
        process = loop.loop_detecting_process
        if process.is_running.value != 1:
            raise RuntimeError("Loop database worker is not running")
        api = importlib.import_module(type(process).__module__)
        task = api.LoopDetectorTask(None, None, api.LoopDetectorTaskType.SAVE,
                                    load_save_path=str(target))
        # The SAVE is enqueued before the backend admits another tracked frame.
        # Its completion wait and disk I/O belong exclusively to the writer.
        process.save_request_completed.value = 0
        process.add_task(task)
    return dict(target=target, document=document, info=info, process=process,
                images=images, format_floats=module.format_floats_for_json)


def write(ticket):
    target = ticket["target"]
    for name, value in (("map.json", ticket["document"]),
                        ("config_info.json", ticket["info"])):
        with (target / name).open("w") as stream:
            json.dump(ticket["format_floats"](value), stream)
            stream.flush()
            os.fsync(stream.fileno())
    if ticket["images"] is not None:
        with (target / "kf_image_map.pkl").open("wb") as stream:
            pickle.dump(ticket["images"], stream)
    process = ticket["process"]
    if process is not None:
        with process.save_request_condition:
            while process.save_request_completed.value == 0:
                if process.is_running.value != 1:
                    raise RuntimeError("Loop database worker stopped during checkpoint")
                process.save_request_condition.wait(.2)
    return target
