"""Read-only observer service; subscribes to existing multiplexed Rover streams.

It never acquires a physical lease, sends a desired state, or restarts Rover.
"""
import asyncio
import contextlib
import hmac
import json
import os
from pathlib import Path
import signal
import time
from aiohttp import web,ClientSession,WSMsgType
from biomerte.app import Application
from biomerte.motion_tracking import MotionRecorder,cached_query,observation_payload
from biomerte.stream_router import decode

def sonar_query():
    import http.client
    import socket
    class UnixHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock=socket.socket(socket.AF_UNIX)
            self.sock.settimeout(5.)
            self.sock.connect(str(Path.home()/".local/share/biome-robot-vision/vision.sock"))
    connection=UnixHTTP("localhost")
    try:
        connection.request("POST","/v1/query",json.dumps(dict(operation="robot.sonar.snapshot",inputs={})),
                           {"Content-Type":"application/json"})
        response=connection.getresponse()
        if response.status!=200:raise ConnectionError("Sonar cache unavailable")
        return json.load(response)
    finally:connection.close()

async def run():
    import hashlib
    controller=Application(str(Path.home()/".local/share/biomerte-controller"))
    secret=controller.config["stream_router_secret"]
    token=hmac.new(secret.encode(),b"rover-site",hashlib.sha256).hexdigest()
    recorder=MotionRecorder(Path.home()/".local/share/biome-rover-motion/motion/observations.sqlite")
    recorder.offer("gap",dict(reason="observer_started",source_mode="read_only_multiplexed_subscriber"))
    @web.middleware
    async def authenticate(request,handler):
        if not hmac.compare_digest(request.headers.get("Authorization",""),"Bearer "+token):
            raise web.HTTPUnauthorized()
        return await handler(request)
    app=web.Application(middlewares=[authenticate],client_max_size=0)
    async def status(request):
        return web.json_response(dict(recorder.status(),observer_mode="read_only_subscriber"))
    async def predict(request):
        try:
            body=await request.json()
            if not isinstance(body,dict) or set(body)-{"pulses","initial_state","step_s"}:
                raise ValueError("Expected pulses, optional initial_state and step_s")
            result=await asyncio.to_thread(recorder.predict,body.get("pulses"),body.get("initial_state"),body.get("step_s",.05))
            return web.json_response(result)
        except (ValueError,TypeError,OverflowError) as error:
            raise web.HTTPBadRequest(text=str(error))
        except RuntimeError as error:
            raise web.HTTPServiceUnavailable(text=str(error))
    app.router.add_get("/api/motion/model",status)
    app.router.add_post("/api/motion/predict",predict)
    stopped=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,stopped.set)
    async def observe(lane):
        while True:
            try:
                async with ClientSession(headers={"Authorization":"Bearer "+token}) as session:
                    async with session.ws_connect("http://127.0.0.1:8093/api/live/"+lane,
                            heartbeat=20,max_msg_size=0) as socket:
                        async for message in socket:
                            received=time.monotonic()
                            if lane=="control" and message.type==WSMsgType.TEXT:
                                value=json.loads(message.data)
                                if value.get("type")=="state" and not value.get("ready"):
                                    recorder.offer("gap",dict(reason="control_not_ready"),received)
                                issued=value.get("issued_utc_ms")
                                if type(issued) in (int,float):
                                    value["host_issue_age_s"]=max(0.,time.time()-issued/1000.)
                                recorder.offer("control",value,received)
                            elif lane=="vision" and message.type in (WSMsgType.TEXT,WSMsgType.BINARY):
                                meta,unused=decode(message.data,4194304)
                                meta=meta.get("snapshot",meta)
                                recorder.offer("vision" if "frame" in meta else "vision_raw",
                                    observation_payload(meta) if "frame" in meta else meta,received)
            except asyncio.CancelledError:raise
            except Exception as error:
                recorder.offer("gap",dict(reason=lane+"_disconnected",error=type(error).__name__))
                await asyncio.sleep(1.)
    async def sonar():
        while True:
            try:
                start=time.monotonic()
                value=await asyncio.to_thread(sonar_query)
                recorder.offer("sonar",dict(value,query_started_monotonic=start))
                interval=value.get("update_rate",{}).get("nominal_per_sensor_period_s") or .26
            except asyncio.CancelledError:raise
            except Exception as error:
                recorder.offer("sensor_error",dict(sensor="sonar",error=type(error).__name__))
                interval=1.
            await asyncio.sleep(interval)
    async def snapshots():
        while True:
            try:
                value=await asyncio.to_thread(cached_query,"robot.vision.snapshot")
                recorder.offer("vision",observation_payload(value))
            except asyncio.CancelledError:raise
            except Exception as error:
                recorder.offer("sensor_error",dict(sensor="coherent_snapshot",error=type(error).__name__))
            await asyncio.sleep(.25)
    runner=web.AppRunner(app);await runner.setup()
    await web.TCPSite(runner,"127.0.0.1",8094).start()
    tasks=[asyncio.create_task(observe("control")),asyncio.create_task(observe("vision")),
           asyncio.create_task(sonar()),asyncio.create_task(snapshots())]
    try:await stopped.wait()
    finally:
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await runner.cleanup()
        recorder.offer("gap",dict(reason="observer_shutdown"))
        await asyncio.to_thread(recorder.close)

if __name__=="__main__":asyncio.run(run())
