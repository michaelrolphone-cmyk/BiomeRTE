# Rear fragment consumption

The rear receiver must assemble a fragmented WebSocket message without adding
a scheduled task for every fragment. On the deployed Python 3.11 runtime,
`asyncio.wait_for(anext(...))` adds task and callback turns even when the
WebSocket library has already buffered the fragments. Competing Python work can
therefore make the application consume old frames while TCP receipt and the
separate clock sender continue.

`messages_with_idle_recovery` uses a timeout in the consuming task and moves its
deadline after each fragment. The existing inactivity limit still applies:
steady progress may take longer than one idle interval, while a stalled partial
message aborts the transport. Clean closure, external cancellation, empty text
and binary messages, and opcode/byte preservation retain their previous behavior.
The delivery and freshness requirements are unchanged.

## Reproduce on the native Python runtime

Use the receiver's existing environment. Supply a retained baseline source file;
the comparison does not import or change the running service.

```sh
PYTHONPATH=providers/robot-vision:providers/robot-vision/tests \
python providers/robot-vision/tests/test_rear_fragment_consumption.py \
  --compare-baseline /absolute/path/to/retained/rear_transport_preview.py \
  --assert-baseline-slow
```

The fixture sends four 23,532-byte messages, each fragmented at 1,400 bytes,
through real loopback WebSockets with compression disabled. It waits until all
72 fragments are buffered, then measures consumption with a bounded competing
8 ms CPU callback. The consumer yields once per complete message. Both readers
must return exactly the same 94,128 bytes; the candidate must fit the unchanged
four-frame budget of 0.8 seconds. The optional negative-control flag also
requires the baseline to miss that budget.

The captured native comparison on Python 3.11.17 and websockets 15.0.1 took
1.876785 seconds for the installed reader and 0.032553 seconds for the candidate.
This demonstrates scheduling amplification under a controlled workload. It does
not identify the source of contention in the running SLAM process, prove a radio
fix, or replace the live camera/LiDAR acceptance test. The Python 3.11 CI job
keeps this regression observable; the existing Python 3.12 suite continues to
exercise the same recovery semantics.
