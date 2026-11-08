"""Opt-in, paced gateway maintenance; never performed on the heartbeat thread."""
import time

MAX_PAGES = 32
BURST_SECONDS = 30.0
REQUEST_TIMEOUT = 120.0
PAGE_INTERVAL = 0.1
BURST_PAUSE = 1.0
IDLE_PAUSE = 2.0
ERROR_PAUSE = 5.0


def run_burst(settings, status, call, clock=time.monotonic, sleep=time.sleep):
    mode = settings.get('checkpoint_mode', 'off')
    if mode not in ('bootstrap', 'maintain'):
        return {'pages': 0, 'outcome': 'disabled'}
    route = 'ha/checkpoint/build' if mode == 'bootstrap' else 'ha/checkpoint/maintain'
    deadline = clock() + BURST_SECONDS
    pages = 0
    while pages < MAX_PAGES and clock() < deadline:
        if status.get('primary') != settings.get('controller_id') or not status.get('ready'):
            return {'pages': pages, 'outcome': 'standby'}
        try:
            # The burst deadline limits admission, not an in-flight page.
            # Cutting its timeout to the remaining window cancels gateway work
            # before cursor persistence and strands its recovery lease.
            response = call(route, {}, timeout=REQUEST_TIMEOUT)
        except Exception:
            return {'pages': pages, 'outcome': 'error'}
        pages += 1
        if mode == 'maintain' and not response.get('enabled'):
            return {'pages': pages, 'outcome': 'disabled'}
        progress = response.get('checkpoint', {}) if mode == 'maintain' else response
        removed = response.get('compacted', {}).get('removed', 0) if mode == 'maintain' else 0
        if progress.get('busy'):
            return {'pages': pages, 'outcome': 'busy'}
        if not progress.get('advanced') and not progress.get('pending') and not removed:
            return {'pages': pages, 'outcome': 'caught_up'}
        head = status.get('head', {}).get('seq')
        if head is not None and progress.get('seq', 0) >= head and not progress.get('pending') and not removed:
            return {'pages': pages, 'outcome': 'caught_up'}
        sleep(min(PAGE_INTERVAL, max(0.0, deadline - clock())))
    return {'pages': pages, 'outcome': 'yield'}


def run(settings, status, call, stopped):
    """Periodic bounded bursts; one request outstanding and primary-only work."""
    while not stopped.is_set():
        result = run_burst(settings, status, call)
        status['checkpoint_worker'] = result
        pause = BURST_PAUSE if result['outcome'] == 'yield' else (
            ERROR_PAUSE if result['outcome'] == 'error' else IDLE_PAUSE)
        stopped.wait(pause)
