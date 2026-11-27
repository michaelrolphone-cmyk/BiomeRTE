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


def signed_command(packet, action, secret):
    motion = packet["spider_motion"]
    sequence = motion["last_accepted_sequence"] + 1
    issued = packet["capture_monotonic_us"]
    mode = "walk" if action in WALKS else "stop" if action == "stop" else "pose"
    name = "none" if mode == "stop" else action
    fields = ("spider_command.v1", motion["session"], packet["receiver_id"],
              sequence, mode, name, issued, issued + 400000)
    signature = hmac.new(secret, "\n".join(map(str, fields)).encode(), hashlib.sha256).hexdigest()
    return dict(zip(("type", "session", "receiver_id", "sequence", "mode", "action",
                     "issued_monotonic_us", "expires_monotonic_us", "signature"),
                    (*fields, signature)))


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
                    print(f"ToF grids {grids}, BNO085 rotation {imu}, UART {motion['uart_ready']}",
                          flush=True)
                if sent_sequence is not None and motion["last_acknowledged_sequence"] == sent_sequence:
                    print(f"ESP8266 acknowledged sequence {sent_sequence}, action {motion['action']}",
                          flush=True)
                    sent_sequence = None
                if (args.action and not action_sent and motion["enabled"] and motion["uart_ready"]
                        and now - interval[0] < 0.15):
                    command = signed_command(packet, args.action, secret)
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
    parser.add_argument("--action", choices=(*WALKS, *POSES, "stop"),
                        help="Send one command. Use with the chassis supported.")
    args = parser.parse_args()
    token = os.environ.get("SPIDER_ROVER_SENSOR_TOKEN", "")
    secret = os.environ.get("SPIDER_ROVER_CONTROL_SECRET", "").encode()
    if len(token) < 32 or (args.action and len(secret) < 32):
        parser.error("Set SPIDER_ROVER_SENSOR_TOKEN and, for motion, SPIDER_ROVER_CONTROL_SECRET")
    try:
        asyncio.run(run(args, token, secret))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
