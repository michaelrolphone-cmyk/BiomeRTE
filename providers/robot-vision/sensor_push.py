"""Authenticated native sensor ingress; device-clock samples are never camera frames."""
import asyncio
import hmac
import json
import logging
import threading
import time
from http import HTTPStatus


class ClockAlignment:
    def __init__(self):
        self.boot = None
        self.bounds = None
        self.observed = None

    def observe(self, boot, sent, received, device_received, device_sent):
        if (not isinstance(boot, str) or not boot or
                type(device_received) is not int or type(device_sent) is not int or
                not 0 <= device_received <= device_sent or received < sent):
            raise ValueError('Invalid clock exchange')
        lower, upper = sent-device_sent/1e6, received-device_received/1e6
        if upper < lower:
            raise ValueError('Invalid clock interval')
        if self.boot == boot and self.bounds is not None:
            drift=max(0.,received-self.observed)*.0001
            shared=(max(lower,self.bounds[0]-drift),min(upper,self.bounds[1]+drift))
            if shared[0]<=shared[1]:lower,upper=shared
        self.boot, self.bounds, self.observed = boot, (lower, upper), received

    def capture_interval(self, packet, received):
        if self.boot != packet['boot_id'] or self.bounds is None:
            raise ValueError('Clock not aligned to sensor boot')
        # Crystal drift broadens the interval between actual clock exchanges.
        drift = max(0., received-self.observed)*.0001
        tick = packet['capture_monotonic_us']/1e6
        lower, upper = tick+self.bounds[0]-drift, tick+self.bounds[1]+drift
        if lower > received:
            raise ValueError('Sensor timestamp is in the future')
        return [lower, min(upper, received)]


class SensorPushReceiver:
    def __init__(self, stream):
        self.stream = stream
        path = stream.service.state_dir/'sensor-push.json'
        self.config = json.loads(path.read_text()) if path.exists() else None
        self.configured = self.config is not None
        self.thread = self.loop = self.server = None
        self.stop = threading.Event()
        self.metrics = dict(packets=0, errors=0, gaps=0, last_sequence=None)
        self._last_diagnostic=0.

    def diagnostic(self,**fields):
        self.metrics.update(fields)
        now=time.monotonic()
        if not fields.get('last_error') and now-self._last_diagnostic<1:return
        path=self.stream.service.state_dir/'sensor-push-status.json'
        temporary=path.with_suffix('.new')
        temporary.write_text(json.dumps(dict(self.metrics,observed_at=time.time())))
        temporary.replace(path)
        self._last_diagnostic=now
        report=getattr(self.stream,'ingress_metrics',None)
        if report is not None:report(dict(self.metrics))

    def start(self):
        if self.configured:
            from sensor_push_process import start_isolated_receiver
            start_isolated_receiver(self)

    def _run(self):
        try:
            asyncio.run(self.serve())
        except Exception as exc:
            self.stream.error = 'sensor_push:'+type(exc).__name__+':'+str(exc)[:120]
            self.diagnostic(state='failed',last_error=self.stream.error)
            logging.getLogger(__name__).exception('Sensor ingress failed')

    async def serve(self):
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        token = self.config['token']
        if not isinstance(token,str) or len(token)<32:
            raise ValueError('Sensor ingress token is missing')

        async def authorize(connection, request):
            if request.path != '/sensors' or not hmac.compare_digest(
                    request.headers.get('Authorization',''), 'Sensor '+token):
                return connection.respond(HTTPStatus.UNAUTHORIZED, 'Unauthorized sensor source')

        async def receive(socket):
            self.diagnostic(state='connected')
            clock = ClockAlignment()
            pending = {}
            previous = None
            async def synchronize():
                while True:
                    # TCP is ordered. Keep the outstanding exchange until its
                    # reply arrives; a slow reply must not lose its clock ID.
                    if not pending:
                        identity = str(time.monotonic_ns())
                        pending[identity] = time.monotonic()
                        await socket.send(json.dumps(dict(type='clock', id=identity)))
                    await asyncio.sleep(1)
            task = asyncio.create_task(synchronize())
            try:
                async for message in socket:
                    received = time.monotonic()
                    value = json.loads(message)
                    if value.get('type') == 'clock':
                        sent = pending.pop(value.get('id'), None)
                        if sent is not None:
                            clock.observe(value['boot_id'],sent,received,
                                          value['received_monotonic_us'],value['served_monotonic_us'])
                            self.diagnostic(state='clock_aligned',clock_round_trip_s=received-sent)
                        continue
                    from capture_packet import validate_packet
                    packet = validate_packet(value, allow_sensor_tick=True)
                    if packet['schema'] != 'biome.sensor_tick.v1' or packet['receiver_id'] != self.config['receiver_id']:
                        raise ValueError('Wrong sensor source')
                    peer=getattr(socket,"remote_address",None)
                    if peer and not getattr(socket,"_route_registered",False):
                        from device_routes import DeviceRoutes
                        DeviceRoutes(self.stream.service.state_dir/"device-routes.sqlite3").observe(packet["receiver_id"],peer[0],{"kind":"camera"})
                        socket._route_registered=True
                    if clock.boot is None:
                        self.diagnostic(state='awaiting_clock')
                        continue
                    sequence = packet.get('stream_sequence')
                    if type(sequence) is not int or (previous is not None and sequence <= previous):
                        raise ValueError('Replayed sensor packet')
                    if previous is not None and sequence != previous+1:
                        self.metrics['gaps'] += sequence-previous-1
                    previous = sequence
                    interval = clock.capture_interval(packet, received)
                    self.stream.publish(packet,received,received,capture_interval=interval)
                    self.metrics.update(packets=self.metrics['packets']+1,last_sequence=sequence,
                                        capture_age_upper_s=received-interval[0],
                                        clock_uncertainty_s=(interval[1]-interval[0])/2)
                    self.diagnostic(state='streaming',last_error=None)
                    samples = ((packet.get('ahrs') or {}).get('ahrs') or {}).get('imu_samples',[])
                    last = samples[-1]['monotonic_us'] if samples else packet['capture_monotonic_us']
                    await socket.send(json.dumps(dict(type='ack',boot_id=packet['boot_id'],
                        stream_sequence=sequence,monotonic_us=last)))
            except Exception as exc:
                self.metrics['errors'] += 1
                self.diagnostic(state='disconnected',last_error=type(exc).__name__+':'+str(exc)[:160])
                raise
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

        async with serve(receive,self.config.get('listen','0.0.0.0'),int(self.config['port']),
                         process_request=authorize,max_size=None,max_queue=None,ping_interval=15):
            self.diagnostic(state='listening')
            while not self.stop.is_set():
                await asyncio.sleep(.1)

    def close(self):
        self.stop.set()
        process=getattr(self,'process',None)
        if process is not None:
            process.join()
            self.thread.join()
            self._delivery_queue.close()
        elif self.thread:
            self.thread.join(timeout=3)
