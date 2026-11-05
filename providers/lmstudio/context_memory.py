"""Choose the user's 32K/64K windows from fresh macOS memory measurements."""
import re
import subprocess
from pathlib import Path


def memory_snapshot(run=subprocess.check_output):
    vm = run(['vm_stat'], text=True)
    page = re.search(r'page size of (\d+) bytes', vm)
    if not page: raise RuntimeError('Cannot measure macOS page size')
    counts = {}
    for label in ('free', 'inactive', 'speculative'):
        found = re.search(r'Pages ' + label + r':\s*(\d+)\.', vm)
        if not found: raise RuntimeError('Cannot measure available macOS memory')
        counts[label] = int(found[1])
    pressure = int(run(['sysctl', '-n', 'kern.memorystatus_vm_pressure_level'], text=True).strip())
    return {'available_bytes': sum(counts.values()) * int(page[1]),
            'pressure_level': pressure, 'available_definition': 'free+inactive+speculative'}


def estimate(model, context, parallel, run=subprocess.check_output):
    cmd = [str(Path.home()/'.lmstudio/bin/lms'), 'load', model,
           '--context-length', str(context), '--estimate-only']
    if parallel is not None: cmd += ['--parallel', str(parallel)]
    output = run(cmd, text=True)
    output = re.sub(r'\x1b\[[0-9;]*m', '', output)
    match = re.search(r'Estimated Total Memory:\s*([\d.]+)\s*(GiB|MiB|GB|MB)', output)
    if not match: raise RuntimeError('LM Studio did not return a memory estimate')
    units = {'GiB':1024**3,'MiB':1024**2,'GB':1000**3,'MB':1000**2}
    return int(float(match[1]) * units[match[2]])


def decision(current, maximum, estimates, snapshot):
    low, high = 32768, 65536
    if maximum < low:
        raise RuntimeError('Requested model does not support the required 32K context')
    available = snapshot['available_bytes']
    present = estimates[current]
    extra_low = max(0, estimates[low] - present)
    # Headroom is the estimated incremental cost of 32K -> 64K, rather than an
    # arbitrary percentage of machine RAM. Preserve that much for load variance.
    headroom = max(0, estimates.get(high, estimates[low]) - estimates[low])
    extra_high = max(0, estimates.get(high, estimates[low]) - present)
    high_ok = maximum >= high and snapshot['pressure_level'] == 1 and available >= extra_high + headroom
    target = high if high_ok else low
    if target == low and available < extra_low:
        raise MemoryError('Insufficient available RAM to load the 32K context')
    return dict(snapshot, target=target, current_context=current,
                estimated_current_bytes=present,
                estimated_32k_bytes=estimates[low],
                estimated_64k_bytes=estimates.get(high),
                estimated_extra_bytes=extra_high if high_ok else extra_low,
                headroom_bytes=headroom,
                reason='64K fits with headroom and normal memory pressure' if high_ok
                       else '32K selected for current available RAM or memory pressure')


def select_context(model, loaded, current, maximum):
    parallel = loaded.get('config', {}).get('parallel')
    sizes = {current, 32768}
    if maximum >= 65536: sizes.add(65536)
    estimates = {size: estimate(model['key'], size, parallel) for size in sizes}
    # Measurement follows estimation so the RAM decision is as fresh as possible.
    return decision(current, maximum, estimates, memory_snapshot())
