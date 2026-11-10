"""Fit live sensor metadata to the existing 16 KiB stream header contract.

Keep physical measurements and their original clocks. Discard duplicated cache
envelopes and diagnostics before trimming radio scan rows. Report omissions;
the complete cache remains available through the discrete telemetry capability.
"""
import copy
import json


def size(value):
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())


def bounded_metadata(metadata, maximum=15872):
    if size(metadata) <= maximum:
        return metadata
    value = copy.deepcopy(metadata)
    ahrs = value.get("ahrs")
    radio = value.get("radio")
    if isinstance(ahrs, dict):
        snapshot = ahrs.pop("snapshot", None)
        if isinstance(snapshot, dict):
            ahrs["cache_timing"] = {key: snapshot[key] for key in (
                "received_at", "received_monotonic", "request_started_monotonic",
                "request_duration_s", "elapsed_since_receive_s", "age_clock", "timing")
                if key in snapshot}
    if isinstance(radio, dict):
        cache = radio.get("telemetry")
        if isinstance(cache, dict):
            firmware = cache.get("telemetry")
            if isinstance(firmware, dict):
                # The RF endpoint repeats the AHRS envelope already carried above.
                firmware.pop("ahrs", None)
            if size(value) > maximum:
                # Per-observation rows here are index/age references, not the
                # actual RF readings. Prefer the complete firmware scan arrays.
                refs = cache.get("observations")
                if isinstance(refs, list):
                    cache["source_observation_count"] = len(refs)
                    cache["stream_reference_rows_omitted"] = len(refs)
                    cache.pop("observations", None)
            if size(value) > maximum and isinstance(firmware, dict):
                firmware.pop("config", None)
    # Health diagnostics duplicate fields available through status capabilities.
    if size(value) > maximum and isinstance(ahrs, dict):
        source = ahrs.pop("source", None)
        if isinstance(source, dict):
            ahrs["source_health"] = {k:source[k] for k in ("state", "error", "enabled") if k in source}
    if isinstance(radio, dict):
        for key in ("model", "learning", "localization"):
            if size(value) <= maximum:
                break
            radio.pop(key, None)
        cache = radio.get("telemetry")
        firmware = cache.get("telemetry") if isinstance(cache, dict) else None
        if isinstance(firmware, dict):
            def scans(node):
                if isinstance(node, dict):
                    for item in node.values():
                        yield from scans(item)
                elif isinstance(node, list) and node and isinstance(node[0], dict):
                    yield node
            arrays = list(scans(firmware.get("wifi"))) + list(scans(firmware.get("ble")))
            omitted = 0
            while size(value) > maximum and any(arrays):
                candidates = [rows for rows in arrays if rows]
                largest = max(candidates, key=lambda rows: size(rows))
                largest.pop()
                omitted += 1
                cache["stream_scan_rows_omitted"] = omitted
            if omitted:
                # Never leave an index pointing beyond a trimmed scan array.
                refs = cache.pop("observations", [])
                cache["stream_reference_rows_omitted"] = max(
                    cache.get("stream_reference_rows_omitted", 0), len(refs))
    if size(value) > maximum:
        raise ValueError("Live metadata exceeds stream header budget")
    return value
