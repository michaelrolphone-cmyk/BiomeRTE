"""Keep sensor transport responsive while SLAM owns the parent interpreter."""
import logging
import multiprocessing
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

class _QueueSink:
    def __init__(self, state_dir, delivery):
        self.service=SimpleNamespace(state_dir=Path(state_dir))
        self.delivery=delivery
        self.error=None
    def publish(self, *args, **kwargs):
        self.delivery.put(('packet',args,kwargs))
    def ingress_metrics(self, metrics):
        self.delivery.put(('metrics',metrics))

def _receiver_process(state_dir, delivery, stop):
    from sensor_push import SensorPushReceiver
    receiver=SensorPushReceiver(_QueueSink(state_dir,delivery))
    receiver.stop=stop
    receiver.metrics.update(execution='independent_process',worker_pid=multiprocessing.current_process().pid)
    try:
        receiver._run()
    finally:
        delivery.put(('end',))
        delivery.close()
        delivery.join_thread()

def _deliver(receiver):
    delivery=receiver._delivery_queue
    while True:
        try:
            message=delivery.get(timeout=.1)
        except queue.Empty:
            if not receiver.process.is_alive():
                receiver.stream.error='sensor_push:ingress_process_exited'
                return
            continue
        if message[0]=='end':return
        if message[0]=='metrics':
            receiver.metrics.update(message[1])
            continue
        _,args,kwargs=message
        try:
            receiver.stream.publish(*args,**kwargs)
            receiver.metrics['delivery_lag_s']=max(0.,time.monotonic()-args[2])
        except Exception as exc:
            receiver.stream.error='sensor_push_delivery:'+type(exc).__name__+':'+str(exc)[:120]
            logging.getLogger(__name__).exception('Sensor delivery failed')

def start_isolated_receiver(receiver):
    # Unbounded transport queue preserves ordered samples during native SLAM
    # pauses; the consumer, never the socket ACK loop, performs enrichment.
    context=multiprocessing.get_context('spawn')
    receiver.stop=context.Event()
    receiver._delivery_queue=context.Queue()
    receiver.process=context.Process(target=_receiver_process,
        args=(str(receiver.stream.service.state_dir),receiver._delivery_queue,receiver.stop),
        name='sensor-websocket-ingress',daemon=True)
    receiver.process.start()
    receiver.thread=threading.Thread(target=_deliver,args=(receiver,),
        name='sensor-websocket-delivery',daemon=True)
    receiver.thread.start()
