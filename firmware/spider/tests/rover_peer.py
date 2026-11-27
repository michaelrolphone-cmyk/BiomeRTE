"""Run a dedicated Rover-compatible socket peer while the spider is on a bench."""

import argparse
import asyncio
import hashlib
import hmac
from http import HTTPStatus
import json
import os
from pathlib import Path
import sys
import time

from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "providers" / "robot-vision"))
from camera_push import receive_camera
from capture_packet import validate_packet
from sensor_push import ClockAlignment


WALKS = ("forward", "backward", "shift_left", "shift_right", "turn_left", "turn_right")
POSES = ("stand", "lie", "sleep", "zero")
JOINT_MIN = (30, 39, 45, 45, 45, 45, 45, 30)
JOINT_MAX = (135, 135, 141, 150, 150, 135, 135, 135)


def load_pose(path, name):
    pose = json.loads(path.read_text())[name]
    angles = pose["angles_deg"]
    duration = pose["duration_ms"]
    stance = pose.get("stance_mask", 0)
    if (not isinstance(angles, list) or len(angles) != 8 or
            any(type(a) is not int or not lo <= a <= hi
                for a, lo, hi in zip(angles, JOINT_MIN, JOINT_MAX)) or
            type(duration) is not int or not 400 <= duration <= 5000 or
            type(stance) is not int or not 0 <= stance <= 15):
        raise ValueError("pose outside firmware limits")
    return {"angles_deg": angles, "duration_ms": duration, "stance_mask": stance}


def signed_command(packet, action, secret, pose=None):
    motion = packet["spider_motion"]
    sequence = motion["last_accepted_sequence"] + 1
    issued = packet["capture_monotonic_us"]
    if pose:
        mode, name = "joint_pose", "custom"
    elif action == "zero_origin":
        mode, name = "origin", "zero"
    elif action == "stop":
        mode, name = "stop", "none"
    else:
        mode, name = ("walk" if action in WALKS else "pose"), action
    fields = ("spider_command.v1", motion["session"], packet["receiver_id"],
              sequence, mode, name, issued, issued + 400000)
    canonical = fields
    if pose:
        canonical += (pose["duration_ms"], pose["stance_mask"], ",".join(map(str, pose["angles_deg"])))
    signature = hmac.new(secret, "\n".join(map(str, canonical)).encode(), hashlib.sha256).hexdigest()
    return {**dict(zip(("type", "session", "receiver_id", "sequence", "mode", "action",
                       "issued_monotonic_us", "expires_monotonic_us", "signature"),
                      (*fields, signature))), **(pose or {})}


async def run(args, token, secret):
    action_sent = False

    def authorize(connection, request):
        if request.path not in ("/camera", "/sensors") or not hmac.compare_digest(
                request.headers.get("Authorization", ""), "Sensor " + token):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized sensor source")

    async def receive(socket):
        nonlocal action_sent
        if socket.request.path == "/camera":
            last_report = 0.0

            def camera(packet, jpeg, interval, received):
                nonlocal last_report
                if received - last_report >= 1:
                    last_report = received
                    print(f"JPEG {packet['width']}x{packet['height']} {len(jpeg)} bytes", flush=True)

            await receive_camera(socket, args.receiver, "front", ClockAlignment(), camera)
            return

        clock = ClockAlignment()
        pending = {}
        sent_sequence = None
        last_report = 0.0
        previous_sequence = 0

        async def synchronize():
            while True:
                identity = str(time.monotonic_ns())
                pending[identity] = time.monotonic()
                if len(pending) > 8:
                    pending.pop(next(iter(pending)))
                await socket.send(json.dumps({"type": "clock", "id": identity}))
                await asyncio.sleep(1)

        task = asyncio.create_task(synchronize())
        try:
            async for message in socket:
                now = time.monotonic()
                value = json.loads(message)
                if value.get("type") == "clock":
                    sent = pending.pop(value["id"], None)
                    if sent is not None:
                        clock.observe(value["boot_id"], sent, now, value["received_monotonic_us"],
                                      value["served_monotonic_us"])
                    continue
                packet = validate_packet(value, allow_sensor_tick=True)
                if (packet["schema"] != "biome.sensor_tick.v1" or
                        packet["receiver_id"] != args.receiver):
                    await socket.close(1008, "Unexpected sensor identity")
                    return
                if clock.boot != packet["boot_id"]:
                    continue
                interval = clock.capture_interval(packet, now)
                sequence = packet["stream_sequence"]
                if type(sequence) is not int or sequence <= previous_sequence:
                    await socket.close(1008, "Replayed sensor tick")
                    return
                previous_sequence = sequence
                motion = packet["spider_motion"]
                if now - last_report >= 1:
                    last_report = now
                    grids = sum(row["valid"] for row in packet["lidar"]["sensors"])
                    imu = packet["bno085"]["game_rotation_wxyz"]["valid"]
                    odom = motion.get("servo_odometry", {})
                    print(f"ToF grids {grids}, BNO085 rotation {imu}, UART {motion['uart_ready']}, "
                          f"odometry {odom.get('status', 'unavailable')}", flush=True)
                if sent_sequence is not None and motion["last_acknowledged_sequence"] == sent_sequence:
                    print(f"ESP8266 acknowledged sequence {sent_sequence}, action {motion['action']}",
                          flush=True)
                    sent_sequence = None
                if sent_sequence is not None and motion.get("last_origin_sequence") == sent_sequence:
                    print(f"Odometry origin reset for sequence {sent_sequence}", flush=True)
                    sent_sequence = None
                pose = getattr(args, "pose", None)
                ready = ((args.action == "zero_origin" or motion["uart_ready"]) and
                         (not pose or motion.get("joint_pose_available")))
                if ((args.action or pose) and not action_sent and motion["enabled"] and ready
                        and now - interval[0] < 0.15):
                    command = signed_command(packet, args.action, secret, pose)
                    action_sent = True
                    sent_sequence = command["sequence"]
                    await socket.send(json.dumps(command))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async with serve(receive, args.listen, args.port, process_request=authorize,
                     max_size=1024 * 1024):
        print(f"Listening on {args.listen}:{args.port} for {args.receiver}", flush=True)
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--receiver", default="spider-camera")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--action", choices=(*WALKS, *POSES, "stop", "zero_origin"),
                          help="Send one command. Use with the chassis supported.")
    commands.add_argument("--pose", metavar="NAME", help="Send one named pose from --pose-file.")
    parser.add_argument("--pose-file", type=Path)
    args = parser.parse_args()
    if args.pose:
        if not args.pose_file:
            parser.error("--pose requires --pose-file")
        try:
            args.pose = load_pose(args.pose_file, args.pose)
        except (OSError, KeyError, ValueError, TypeError) as error:
            parser.error(f"Invalid pose file: {error}")
    token = os.environ.get("SPIDER_ROVER_SENSOR_TOKEN", "")
    secret = os.environ.get("SPIDER_ROVER_CONTROL_SECRET", "").encode()
    if len(token) < 32 or ((args.action or args.pose) and len(secret) < 32):
        parser.error("Set SPIDER_ROVER_SENSOR_TOKEN and, for motion, SPIDER_ROVER_CONTROL_SECRET")
    try:
        asyncio.run(run(args, token, secret))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
