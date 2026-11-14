"""Preserve complete live telemetry without an artificial metadata size cap."""
import copy
import json

def size(value):
    return len(json.dumps(value,separators=(",",":"),allow_nan=False).encode())

def bounded_metadata(metadata, maximum=None):
    """Compatibility entry point; maximum is ignored and all fields are retained."""
    return copy.deepcopy(metadata)
