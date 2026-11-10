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
    if size(metadata) <= maximum or not any(key in metadata for key in ("ahrs", "radio")):
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
                # Retain RF age/index bindings but omit repeated long source IDs.
                refs = cache.get("observations")
                if isinstance(refs, list):
                    cache["source_observation_count"] = len(refs)
                    cache["observations"] = [{k: row[k] for k in
                        ("radio", "index", "effective_age_ms") if k in row}
                        for row in refs if isinstance(row, dict)]
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
            groups = {kind: list(scans(firmware.get(kind))) for kind in ("wifi", "ble")}
            arrays = groups["wifi"] + groups["ble"]
            omitted = 0
            while size(value) > maximum and any(arrays):
                candidates = [rows for rows in arrays if rows]
                largest = max(candidates, key=lambda rows: size(rows))
                average = max(1, size(largest) // len(largest))
                excess = size(value) - maximum
                count = min(len(largest), max(1, (excess + average - 1) // average))
                del largest[-count:]
                omitted += count
                cache["stream_scan_rows_omitted"] = omitted
            if omitted:
                # Never leave an index pointing beyond a trimmed scan array.
                refs = cache.get("observations", [])
                limits = {kind: max((len(rows) for rows in groups[kind]), default=0)
                          for kind in groups}
                kept = [row for row in refs if row.get("radio") in limits
                        and isinstance(row.get("index"), int)
                        and row["index"] < limits[row["radio"]]]
                cache["observations"] = kept
                cache["stream_reference_rows_omitted"] = len(refs)-len(kept)
    if size(value) > maximum:
        raise ValueError("Live metadata exceeds stream header budget")
    return value
