"""Short-lived, robot-bound commands for an authenticated spider sensor socket."""
import hashlib
import hmac
import json
import queue
import time

COMMAND = "robot.motion.command"
STATUS = "robot.motion.status"
WIRE = "spider_command.v1"
MAX_SAMPLE_AGE_S = .15
LEASE_US = 400000


def validate_intent(inputs, identity, control):
    allowed = {"robot_id", "mode", "action", "angles_deg", "duration_ms", "stance_mask"}
    if set(inputs) - allowed or inputs.get("robot_id") != identity["robot_id"]:
        raise ValueError("motion_robot_identity_required")
    mode, action = inputs.get("mode"), inputs.get("action")
    if (not isinstance(mode, str) or not isinstance(action, str) or
            action not in control["modes"].get(mode, ())):
        raise ValueError("unknown_motion_mode_or_action")
    additional = set(inputs) - {"robot_id", "mode", "action"}
    if mode != "joint_pose":
        if additional:
            raise ValueError("unexpected_motion_fields")
        return {"mode": mode, "action": action}
    angles, duration = inputs.get("angles_deg"), inputs.get("duration_ms")
    stance = inputs.get("stance_mask", 0)
    constraints = control["joint_pose"]
    if (not isinstance(angles, list) or len(angles) != 8 or
            any(type(a) is not int or not lo <= a <= hi
                for a, lo, hi in zip(angles, constraints["min_deg"], constraints["max_deg"])) or
            type(duration) is not int or not constraints["duration_ms"][0] <= duration <= constraints["duration_ms"][1] or
            type(stance) is not int or not 0 <= stance <= 15):
        raise ValueError("invalid_joint_pose")
    return {"mode": mode, "action": action, "angles_deg": list(angles),
            "duration_ms": duration, "stance_mask": stance}


def capture_binding(packet):
    motion = packet.get("spider_motion")
    if not isinstance(motion, dict):
        return None
    session = motion.get("session")
    if (motion.get("enabled") is not True or not isinstance(session, str) or len(session) != 16
            or any(c not in "0123456789abcdef" for c in session)):
        return None
    return packet["receiver_id"], packet["boot_id"], session


def command_ready(packet, mode):
    motion = packet["spider_motion"]
    return (mode == "origin" or motion.get("uart_ready") is True) and (
        mode != "joint_pose" or motion.get("joint_pose_available") is True)


def sign(packet, desired, sequence, secret):
    binding = capture_binding(packet)
    if binding is None or not 0 < sequence < 2**32:
        raise ValueError("invalid_motion_binding")
    receiver, _, session = binding
    issued = packet["capture_monotonic_us"]
    result = {"type": WIRE, "session": session, "receiver_id": receiver, "sequence": sequence,
              **desired, "issued_monotonic_us": issued, "expires_monotonic_us": issued+LEASE_US}
    fields = [WIRE, session, receiver, sequence, desired["mode"], desired["action"], issued, issued+LEASE_US]
    if desired["mode"] == "joint_pose":
        fields += [desired["duration_ms"], desired["stance_mask"], ",".join(map(str, desired["angles_deg"]))]
    result["signature"] = hmac.new(secret.encode(), "\n".join(map(str, fields)).encode(), hashlib.sha256).hexdigest()
    return result


def request(service, operation, inputs):
    stream = service.sensor_ticks
    snapshot = stream._push_snapshot
    receiver = getattr(stream, "push", None)
    outbox = getattr(receiver, "motion_outbox", None)
    packet, interval = snapshot if snapshot else ({}, None)
    binding = capture_binding(packet)
    fresh = bool(binding and interval and 0 <= time.monotonic()-interval[0] <= MAX_SAMPLE_AGE_S)
    identity = service.robot_identity
    if operation == STATUS:
        return {**identity, "availability": "available" if fresh else "unavailable",
                "motion": packet.get("spider_motion") if fresh else None,
                "command_route_ready": fresh and outbox is not None and command_ready(packet, "walk")}
    desired = validate_intent(inputs, identity, service.robot_definition["control"])
    if not fresh or outbox is None or not command_ready(packet, desired["mode"]):
        return {**identity, "availability": "unavailable", "reason": "fresh_motion_socket_required"}
    try:
        outbox.put_nowait((binding, desired, time.monotonic() + MAX_SAMPLE_AGE_S))
    except queue.Full:
        return {**identity, "availability": "unavailable", "reason": "motion_command_pending"}
    return {**identity, "availability": "available", "delivery": "queued"}


async def deliver(socket, outbox, packet, interval, secret, last):
    try:
        binding, desired, deadline = outbox.get_nowait()
    except queue.Empty:
        return last
    current = capture_binding(packet)
    now = time.monotonic()
    if (current != binding or now > deadline or now-interval[0] > MAX_SAMPLE_AGE_S or
            not command_ready(packet, desired["mode"])):
        return last
    accepted = packet["spider_motion"].get("last_accepted_sequence")
    if type(accepted) is not int or not 0 <= accepted < 2**32-1:
        return last
    sequence = max(accepted, last[1] if last[0] == binding else 0) + 1
    if sequence >= 2**32:
        return last
    await socket.send(json.dumps(sign(packet, desired, sequence, secret)))
    return binding, sequence
