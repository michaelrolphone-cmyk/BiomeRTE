"""Opt-in Python thread locations without frame values or application locks.

This is a best-effort Python snapshot, not a native stack or a health verdict.
The calling interpreter must be able to run; a GIL stall can delay this request.
No frames are retained after the response has been copied into scalar records.
"""
import json
import os
import sys
import threading
import time
from itertools import islice

MAX_THREADS = 64
MAX_FRAMES_PER_THREAD = 32
MAX_JSON_BYTES = 65536
MAX_NAME_CHARS = 128
MAX_FILENAME_CHARS = 512
MAX_FUNCTION_CHARS = 128


def _json_size(value):
    # Match handler_for()._respond's default JSON spacing and ASCII escaping.
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def _text(value, limit):
    if not isinstance(value, str):
        return None, False
    return value[:limit], len(value) > limit


def thread_stack_snapshot():
    started = time.monotonic()
    current = sys._current_frames()
    frame = None
    try:
        identities = list(islice(current, MAX_THREADS))
        wanted = set(identities)
        names = {thread.ident: thread.name for thread in threading.enumerate()
                 if thread.ident in wanted}
        result = {
            "schema": "biome.vision.thread-stacks.v1",
            "pid": os.getpid(),
            "observation_started_monotonic_s": started,
            "observation_finished_monotonic_s": started,
            "frames_order": "innermost_first",
            "threads_total": len(current),
            "threads_recorded": 0,
            "limits": {"threads": MAX_THREADS, "frames_per_thread": MAX_FRAMES_PER_THREAD,
                       "json_bytes": MAX_JSON_BYTES, "name_chars": MAX_NAME_CHARS,
                       "filename_chars": MAX_FILENAME_CHARS,
                       "function_chars": MAX_FUNCTION_CHARS},
            "truncated": False,
            "thread_limit_reached": len(current) > MAX_THREADS,
            "byte_limit_reached": False,
            "threads": [],
        }
        # Reserve room for final timestamp/count changes. Row/frame accounting is
        # incremental; do not repeatedly serialize the entire growing snapshot.
        budget = MAX_JSON_BYTES - _json_size(result) - 128
        used = 0
        for ident in identities:
            name, clipped = _text(names.get(ident), MAX_NAME_CHARS)
            row = {"ident": ident, "name": name, "frames": [],
                   "frames_truncated": False, "strings_truncated": clipped}
            row_size = _json_size(row) + (2 if result["threads"] else 0)
            if used + row_size > budget:
                result["byte_limit_reached"] = True
                break
            result["threads"].append(row)
            used += row_size
            frame = current[ident]
            while frame is not None and len(row["frames"]) < MAX_FRAMES_PER_THREAD:
                code = frame.f_code
                filename, path_clipped = _text(code.co_filename, MAX_FILENAME_CHARS)
                function, function_clipped = _text(code.co_name, MAX_FUNCTION_CHARS)
                location = {"filename": filename, "function": function, "lineno": frame.f_lineno}
                added = _json_size(location) + (2 if row["frames"] else 0)
                if used + added > budget:
                    result["byte_limit_reached"] = True
                    break
                row["frames"].append(location)
                used += added
                row["strings_truncated"] |= path_clipped or function_clipped
                frame = frame.f_back
            row["frames_truncated"] = frame is not None
            if result["byte_limit_reached"]:
                break
        result["threads_recorded"] = len(result["threads"])
        result["truncated"] = (result["thread_limit_reached"] or result["byte_limit_reached"] or
                               any(row["frames_truncated"] or row["strings_truncated"]
                                   for row in result["threads"]))
        result["observation_finished_monotonic_s"] = time.monotonic()
        # A final wire-size guard includes the envelope and any timestamp growth.
        while _json_size(result) > MAX_JSON_BYTES and result["threads"]:
            result["threads"].pop()
            result["threads_recorded"] = len(result["threads"])
            result["byte_limit_reached"] = result["truncated"] = True
        return result
    finally:
        frame = None
        current.clear()
