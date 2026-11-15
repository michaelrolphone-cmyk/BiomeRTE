"""Authenticated camera WebSocket messages from one registered ESP32."""
import asyncio
import json
import struct
import time
from capture_packet import validate_packet


def unpack_capture(message, receiver_id, role):
    if not isinstance(message, bytes) or len(message) < 9:
        raise ValueError('Expected a binary camera frame')
    size = struct.unpack_from('!I', message)[0]
    boundary = 4 + size
    if size == 0 or boundary + 4 > len(message):
        raise ValueError('Incomplete camera message')
    packet = validate_packet(json.loads(message[4:boundary]))
    if packet['receiver_id'] != receiver_id or packet.get('camera_id') != role:
        raise ValueError('Unexpected camera identity')
    jpeg = message[boundary:]
    if not jpeg.startswith(bytes.fromhex('ffd8')) or not jpeg.endswith(bytes.fromhex('ffd9')):
        raise ValueError('Incomplete camera JPEG')
    return packet, jpeg


async def receive_camera(socket, receiver_id, role, clock, deliver, boot_hint=None, preview=None, messages=None):
    pending = {}
    ready_boot=None
    async def synchronize():
        nonlocal ready_boot
        while True:
            identity=str(time.monotonic_ns())
            pending[identity]=time.monotonic()
            for old in list(pending)[:-16]:pending.pop(old,None)
            await socket.send(json.dumps({'type':'clock','id':identity}))
            hint=boot_hint() if boot_hint else None
            if hint and hint!=ready_boot:
                await socket.send(json.dumps({'type':'camera_ready','boot_id':hint}))
                ready_boot=hint
            await asyncio.sleep(1)
    sync = asyncio.create_task(synchronize())
    try:
        async for message in (messages if messages is not None else socket):
            received = time.monotonic()
            if isinstance(message, str):
                reply = json.loads(message)
                sent = pending.pop(reply.get('id'), None)
                if reply.get('type') == 'clock' and sent is not None:
                    clock.observe(reply['boot_id'], sent, received,
                                  reply['received_monotonic_us'], reply['served_monotonic_us'])
                    await socket.send(json.dumps({'type':'camera_ready','boot_id':clock.boot}))
                continue
            packet, jpeg = unpack_capture(message, receiver_id, role)
            if getattr(clock,'boot',None)==packet['boot_id'] or preview is None:
                interval = clock.capture_interval(packet, received)
                deliver(packet, jpeg, interval, received)
            else:
                preview(packet,jpeg,received)
            await socket.send(json.dumps({'type':'camera_ack','frame_id':packet['frame_id']}))
    finally:
        sync.cancel()
        await asyncio.gather(sync,return_exceptions=True)
