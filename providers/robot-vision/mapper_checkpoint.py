"""Execute immutable graph capture on the local mapper's FIFO owner thread."""
from concurrent.futures import Future, TimeoutError
import functools
import time

class _CaptureCall:
    def __init__(self, function):
        self.function = function
        self.future = Future()

def install_owner_capture(mapper_class):
    original = mapper_class.pop_keyframe
    if getattr(original, "_biome_owner_capture", False):
        return
    @functools.wraps(original)
    def pop(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        if isinstance(value, tuple) and len(value) == 4 and isinstance(value[1], _CaptureCall):
            call = value[1]
            try:
                call.future.set_result(call.function())
            except BaseException as exc:
                call.future.set_exception(exc)
            return None
        return value
    pop._biome_owner_capture = True
    mapper_class.pop_keyframe = pop

def capture_on_mapper(mapper, function):
    if not getattr(type(mapper).pop_keyframe, "_biome_owner_capture", False):
        raise RuntimeError("Checkpoint owner adapter must be installed before mapper startup")
    call = _CaptureCall(function)
    # Admission and stop requests share the native stop mutex. A stop drains
    # queued work before acknowledging, so release() cannot discard this call.
    while True:
        with mapper.stop_mutex:
            if not mapper.is_running:
                raise RuntimeError("Local mapper stopped before checkpoint admission")
            if not mapper.stop_requested and not mapper.stopped:
                mapper.queue.put((None, call, None, None))
                break
        time.sleep(.005)
    with mapper.queue_condition:
        mapper.queue_condition.notify_all()
    while True:
        try:
            return call.future.result(timeout=.1)
        except TimeoutError:
            if call.future.done():
                return call.future.result()
            if not mapper.is_running:
                raise RuntimeError("Local mapper stopped before checkpoint completion")
