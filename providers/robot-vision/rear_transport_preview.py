"""Arrival-timed rear displays while the ESP capture clock is being aligned."""
import asyncio
import datetime


def publish_sensor(ingress, packet, received):
    """Give the native receiver the newest preview without waiting on mapping locks."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _publish_sensor(ingress, packet, received)

    ingress._pending_preview = (packet, received)
    if getattr(ingress, "_preview_task", None) is None:
        ingress._preview_task = loop.create_task(_drain_previews(ingress))


async def _drain_previews(ingress):
    try:
        while ingress._pending_preview is not None and not ingress.stop.is_set():
            item, ingress._pending_preview = ingress._pending_preview, None
            try:
                await asyncio.to_thread(_publish_sensor, ingress, *item)
            except (KeyError, TypeError, ValueError) as error:
                ingress.error = "rear_preview:" + str(error)[:120]
    finally:
        ingress._preview_task = None


def _publish_sensor(ingress,packet,received):
    previous=getattr(ingress,'_preview_identity',None)
    identity=(packet['boot_id'],packet['capture_monotonic_us'])
    if previous and previous[0]==identity[0] and previous[1]>=identity[1]:return
    ingress._preview_identity=identity
    svc=ingress.service
    data=dict(type='telemetry',camera_id='rear',source_id='rear-camera',frame_id=packet['frame_id'],
              sensor_tick=True,image_available=False,sensor_delivery='websocket_push',sensor_packet=packet,
              processed_monotonic=received,processed_monotonic_basis='sensor_packet_receive_time',
              capture_timing='device_ticks_only',map_id=svc.map_id,map_revision=svc.revision,session_id=svc.session_id,
              lidar=packet.get('lidar'),ftm=packet.get('ftm'),uart=packet.get('uart'))
    if packet.get('lidar') and getattr(svc,'lidar',None):
        with svc.lidar.lock:
            if svc.lidar.raw_latest and svc.lidar.raw_latest[2] > received:
                return
            svc.lidar.raw_latest=(packet,None,received)
        data['lidar_fusion']=svc.lidar.status()
    svc.rear_live_frames.publish(data,b'')


def publish_image(ingress,packet,jpeg,received):
    svc=ingress.service
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    data=dict(type='frame',camera_id='rear',source_id='rear-camera',frame_id=packet['frame_id'],image_available=True,
              capture_timing='device_ticks_only',source_timestamp=None,processing_timestamp=None,
              receive_decode_timestamp=now,receive_decode_clock='mac_utc_after_packet_validation',processed_monotonic=received,
              processed_monotonic_basis='camera_packet_receive_time',map_id=svc.map_id,map_revision=svc.revision,
              session_id=svc.session_id,calibration_revision=(ingress.rig.config['rear'].get('calibration') or {}).get('calibration_id'),
              width=packet['width'],height=packet['height'],processing_state='awaiting_capture_clock')
    svc.rear_live_frames.publish(data,jpeg)

async def messages_with_idle_recovery(socket, idle_seconds):
    """A fragment advances the idle deadline even on a bandwidth-limited camera."""
    from websockets.exceptions import ConnectionClosedOK

    while True:
        fragments = socket.recv_streaming()
        chunks = []
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(fragments), idle_seconds)
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
        except ConnectionClosedOK:
            return
        except asyncio.TimeoutError as error:
            socket.transport.abort()
            raise ConnectionError("rear device stopped providing messages") from error
        finally:
            await fragments.aclose()

        # recv_streaming preserves the text/binary opcode, including empty messages.
        yield chunks[0][:0].join(chunks)
