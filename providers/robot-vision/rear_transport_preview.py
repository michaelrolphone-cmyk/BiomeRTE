"""Arrival-timed rear displays while the ESP capture clock is being aligned."""
import datetime


def publish_sensor(ingress,packet,received):
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
        with svc.lidar.lock:svc.lidar.raw_latest=(packet,None,received)
        data['lidar_fusion']=svc.lidar.status()
    svc.rear_live_frames.publish(data,b'')


def publish_image(ingress,packet,jpeg,received):
    svc=ingress.service
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    data=dict(type='frame',camera_id='rear',source_id='rear-camera',frame_id=packet['frame_id'],image_available=True,
              capture_timing='device_ticks_only',receive_decode_timestamp=now,processed_monotonic=received,
              processed_monotonic_basis='camera_packet_receive_time',map_id=svc.map_id,map_revision=svc.revision,
              session_id=svc.session_id,calibration_revision=(ingress.rig.config['rear'].get('calibration') or {}).get('calibration_id'),
              width=packet['width'],height=packet['height'],processing_state='awaiting_capture_clock')
    svc.rear_live_frames.publish(data,jpeg)
