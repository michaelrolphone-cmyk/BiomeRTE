# Local publication-stage trace

This diagnostic is off by default. It records bounded scalar timings; it does
not change camera or sensor algorithms, metadata, freshness, queues, pacing,
GC settings, or the normal status response.

## Explicit control

Use the existing Unix socket's POST /v1/query transport:

    {"operation":"robot.vision.trace","inputs":{"action":"arm","epoch":"unique-operator-nonce","expected_pid":12345,"duration_s":135}}

The operation is rejected by a non-AF_UNIX HTTP connection and is not advertised
as a public capability. The operator must bind source hashes, process start,
receiver boot and the independent observer outside this diagnostic. Returned
source stage names are tied to that exact installed source manifest, not an
automatic claim that current disk bytes are loaded code.

Run the ordinary observer without polling the trace. After it exits, freeze:

    {"operation":"robot.vision.trace","inputs":{"action":"freeze","epoch":"unique-operator-nonce","expected_pid":12345}}

Freeze returns the retained report. A same-epoch read reconciles an uncertain
freeze; never create a new epoch to retry an uncertain action:

    {"operation":"robot.vision.trace","inputs":{"action":"read","epoch":"unique-operator-nonce","expected_pid":12345}}

An identical arm retry returns the existing epoch, original start/deadline and
current state, including frozen state. A mismatched duration or PID is rejected.
A different epoch is rejected until the preceding epoch is frozen and all
previous active with-blocks have completed their diagnostic cleanup. This
prevents retaining arbitrarily many old epochs through stuck application calls.

The default window is 135 seconds; the hard maximum is 150. A daemon Timer
freezes independently of a later read. It is Python-scheduled and may run late
during GIL contention. New span and GC-start admission always checks the nominal
deadline. The report retains both that deadline and actual freeze time, and
never substitutes the deadline for a real late completion timestamp.
Service shutdown cancels the owned timer and removes only the owned GC callback.
The diagnostic is PID-scoped; a forked child does not inherit active recording.

## What is measured

- Front raw metadata construction.
- Common publication observer, conditional LiDAR status, derivations, inventory,
  packet serialization, and per-source subscriber fanout.
- Derivation RLock wait versus held time, then TOF copy/cache, orientation,
  display fitting and projection.
- LiDAR.status state-lock wait versus held copy time.
- Backend start and close around submap recovery/start failure.
- GC start/stop phase, generation, thread/native ID and the current traced context.

The real application with-blocks, call order and exception propagation are
preserved. No diagnostic lock spans application work. The GC callback takes no
locks, walks no frames, retains no payload and performs no logging or I/O.

Enrich has one instrumented acquisition site. LiDAR state has other acquisition
sites: a missing owner token means unknown/uninstrumented owner, not a free
lock. Owner tokens are observations within one epoch; they are not OS lock
inspection. A wait span includes scheduling delay before the acquired marker.

Frame identity uses bounded scalar camera/source/frame/receiver/boot/map IDs and
existing source timestamps. Missing values stay missing; clipped strings are
marked. No JPEG, head arrays, TOF object, authentication data or exception text
is retained. An independently called LiDAR.status has no fabricated frame ID.

Fanout is the last frame-attributed boundary. The trace does not measure a UDS
writer's completion, identify a writer's frame, or instrument another process's
leased publisher/sequence store.

## Bounds and interpretation

The report declares every limit: 128 active spans, 64 active thread stacks,
depth16, 2,048 slow spans, 2,048 GC phase events, 96 characters per identity
string and an 8 MiB export ceiling. Slow means at least20 ms; all admitted
completed spans contribute to per-stage totals. Exceptions and late completions
are retained regardless of that threshold. Ring overwrites, admission overflow,
instrumentation errors and export drops remain explicit.

Stage totals are nested/inclusive, so do not add them into exclusive process
wall time or CPU. Wall time includes descheduling and GC; thread CPU is measured
only by the same executing thread. Active spans have no invented end or zero
duration. Freeze preserves them as incomplete even if their finally runs later.
A GC start without a captured stop is incomplete. Late stop timestamps and
freeze lag remain visible. A callback already executing at freeze is flagged.

A snapshot without completed slow spans does not establish absence of delay if
there are active/incomplete rows, overflows, a missed window, or a stalled Python
interpreter. GC overlap and common lock waiting are evidence to correlate with
the independent viewer timeline, not proof of causality by themselves.
