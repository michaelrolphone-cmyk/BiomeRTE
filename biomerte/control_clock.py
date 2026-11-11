"""Bounded UTC estimates for control deadlines, independent of host wall time.

UDP sampling runs in worker threads. Motion never refreshes from a clock sample;
only a new input can issue a new deadline. The OS clock is left untouched.
"""
import asyncio
import contextlib
import itertools
import socket
import struct
import time

NTP_EPOCH = 2208988800
SERVERS = ('time.apple.com', 'time.google.com', 'pool.ntp.org')


def ntp_sample(host):
    request = bytearray(48)
    request[0] = 0x23
    # Originate validation binds the reply to this request.
    stamp = time.time() + NTP_EPOCH
    struct.pack_into('!II', request, 40, int(stamp), int((stamp % 1) * 2**32))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wire:
        wire.settimeout(3)
        start = time.monotonic()
        wire.connect((host, 123))
        wire.send(request)
        reply = wire.recv(512)
        end = time.monotonic()
    if (len(reply) < 48 or reply[24:32] != request[40:48] or
            reply[0] & 7 != 4 or (reply[0] >> 3) & 7 not in (3, 4) or
            reply[0] >> 6 == 3 or not 1 <= reply[1] <= 15):
        raise ValueError('Invalid NTP response')
    words = struct.unpack('!12I', reply[:48])
    utc = lambda i: words[i] - NTP_EPOCH + words[i+1] / 2**32
    received, transmitted = utc(8), utc(10)
    network = end - start - (transmitted - received)
    root_delay = struct.unpack('!i', reply[4:8])[0] / 65536
    radius = max(0, network)/2 + max(0, root_delay)/2 + words[2]/65536 + .005
    if transmitted < received or network < -.002 or radius > .1:
        raise ValueError('NTP uncertainty exceeds control budget')
    # UTC-minus-monotonic interval, not an adjustment to the OS clock.
    center = (received + transmitted - start - end)/2
    return center - radius, center + radius


def consensus(samples):
    """Require overlapping intervals from at least two independent sources."""
    intervals = [s for s in samples if not isinstance(s, BaseException)]
    for count in range(len(intervals), 1, -1):
        choices = []
        for group in itertools.combinations(intervals, count):
            lower, upper = max(s[0] for s in group), min(s[1] for s in group)
            if lower <= upper and upper-lower <= .15:
                choices.append((lower, upper))
        if choices:
            return min(choices, key=lambda s: s[1]-s[0])
    raise RuntimeError('Control UTC lacks agreeing time sources')


class ControlClock:
    def __init__(self, servers=SERVERS, sampler=ntp_sample, monotonic=time.monotonic):
        if len(set(servers)) < 3:
            raise ValueError('Control clock requires three distinct NTP sources')
        self.servers, self.sampler, self.monotonic = tuple(servers), sampler, monotonic
        self.interval = None
        self.sampled_at = None
        self.task = None

    @property
    def ready(self):
        return self.sampled_at is not None and 0 <= self.monotonic()-self.sampled_at <= 90

    def bound(self, upper):
        if not self.ready:
            raise RuntimeError('Control UTC is not synchronized')
        now = self.monotonic()
        # Conservative 50 ppm drift allowance between refreshes. Issuers use
        # the lower UTC bound; validators use the upper bound. Independent
        # estimates cannot mistake a fresh command for future-dated input.
        drift = (now-self.sampled_at)*.00005
        return now + self.interval[1 if upper else 0] + (drift if upper else -drift)

    def lower(self):
        return self.bound(False)

    def upper(self):
        return self.bound(True)

    def status(self):
        return dict(ready=self.ready, sample_age_seconds=None if self.sampled_at is None else
                    self.monotonic()-self.sampled_at, uncertainty_ms=None if self.interval is None else
                    (self.interval[1]-self.interval[0])*500)

    async def refresh(self):
        values = await asyncio.gather(*(asyncio.to_thread(self.sampler, s) for s in self.servers),
                                      return_exceptions=True)
        interval = consensus(values)
        self.interval, self.sampled_at = interval, self.monotonic()

    async def start(self):
        await self.refresh()
        async def renew():
            while True:
                await asyncio.sleep(30)
                try:
                    await self.refresh()
                except (OSError, ValueError, RuntimeError):
                    # Retain only the bounded-age estimate. Failure cannot
                    # renew clock readiness or any desired output.
                    pass
        self.task = asyncio.create_task(renew())
        return self

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
