"""Bind a vision instance and its persistent state to one robot."""
import fcntl
import json
import re
from pathlib import Path

MODELS = {"rover": "rover-body-model.json", "lafvin-spider": "spider-body-model.json"}
SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


def robot_identity(config):
    if "robot" not in config:
        return {"robot_id": "rover", "model": "rover"}
    robot = config["robot"]
    if not isinstance(robot, dict) or set(robot) != {"id", "model"}:
        raise ValueError("robot_requires_id_and_model")
    if any(not isinstance(v, str) or not SLUG.fullmatch(v) for v in robot.values()):
        raise ValueError("invalid_robot_identity")
    if robot["id"] == "rover" and robot["model"] != "rover":
        raise ValueError("legacy_rover_identity_reserved")
    return {"robot_id": robot["id"], "model": robot["model"]}


def bind_robot_state(state, identity):
    """Prevent opening another robot's state, including unmigrated Rover maps."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "robot-instance.json"
    lock = state / "robot-instance.lock"
    # The service lock is held in the CLI. Also cover callers using VisionService directly.
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if marker.exists():
            if json.loads(marker.read_text()) != identity:
                raise ValueError("state_belongs_to_another_robot")
        elif identity["robot_id"] != "rover":
            if any((state / name).exists() for name in
                   ("runtime.json", "observations", "world.sqlite3", "lidar-map.sqlite3")):
                raise ValueError("existing_unbound_robot_state")
            from vision_config import atomic_json
            atomic_json(marker, identity)


def model_definition(state, identity, *, use_builtin=True):
    override = Path(state) / "robot-model.json"
    if override.exists():
        model = json.loads(override.read_text())
    else:
        name = MODELS.get(identity["model"]) if use_builtin else None
        if not name:
            raise FileNotFoundError("custom_robot_model_required")
        model = json.loads(Path(__file__).with_name(name).read_text())
    if (not isinstance(model, dict) or model.get("schema_version") != 1 or
            not isinstance(model.get("model_id"), str) or not model["model_id"] or
            type(model.get("revision")) is not int or model["revision"] < 1):
        raise ValueError("invalid_robot_model_identity")
    # Legacy rover-model overrides predate the explicit profile field.
    if identity["model"] != "rover" and model.get("profile") != identity["model"]:
        raise ValueError("robot_model_profile_mismatch")
    if model.get("profile", identity["model"]) != identity["model"]:
        raise ValueError("robot_model_profile_mismatch")
    if identity["robot_id"] != "rover":
        camera = model.get("camera") or {}
        if not isinstance(camera, dict) or not isinstance(model.get("control", {}), dict):
            raise ValueError("invalid_robot_model")
        if camera.get("T_robot_camera") is not None:
            from navigation_frames import rigid_transform
            rigid_transform(camera["T_robot_camera"])
    json.dumps(model, allow_nan=False)
    return model


def prepare_robot(config, state, identity):
    """Apply robot-specific defaults before any tracker or state store is opened."""
    if identity["robot_id"] == "rover":
        return config
    definition = model_definition(state, identity)
    camera = config["camera"]
    receiver = camera.get("source_id")
    if not isinstance(receiver, str) or not receiver:
        raise ValueError("robot_camera_identity_required")
    if camera.get("capture_transport") == "websocket_push":
        ingress = json.loads((state / "sensor-push.json").read_text())
        if ingress.get("receiver_id") != receiver:
            raise ValueError("robot_camera_ingress_identity_mismatch")
        if type(ingress.get("port")) is not int or not 1 <= ingress["port"] <= 65535:
            raise ValueError("robot_camera_ingress_port_required")
        if ingress.get("motion_enabled") is True:
            secret = ingress.get("motion_secret")
            if (definition.get("control", {}).get("transport") != "spider_command.v1"
                    or not isinstance(secret, str) or len(secret.encode()) < 32):
                raise ValueError("spider_motion_secret_required")
    rig_path = state / "sensor-rig.json"
    if rig_path.is_file():
        from lidar_fusion import validate_config
        validate_config(json.loads(rig_path.read_text()), identity["robot_id"])
    config.setdefault("ultrasonic", {"enabled": False})
    config.setdefault("ahrs", {"enabled": False})
    config.setdefault("calibration", {
        "calibration_id": identity["robot_id"] + "-camera-unmeasured",
        "width": camera.get("expected_width", 1280),
        "height": camera.get("expected_height", 720),
        "validated": False, "provenance": "intrinsics_unmeasured", "camera_to_robot": None,
    })
    config.setdefault("tof", {}).setdefault("state_path", str(state / "tof" / "latest.json"))
    return config
