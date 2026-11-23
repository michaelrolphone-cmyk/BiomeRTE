"""Default-off IP_TOS trial for authenticated, validated rear connections.

This changes only an accepted IPv4 socket. Stored option readback is evidence of
the request, not proof of wire DSCP or Wi-Fi queue selection.
"""
import socket
import time


class RearReturnPathCS5:
    def __init__(self, *, enabled, receiver_id, role):
        self.enabled = enabled is True
        self.receiver_id = receiver_id
        self.role = role
        self.attempted = False

    def after_validated_packet(self, connection, packet):
        """Called by the rear listener only after its existing packet validator."""
        if not self.enabled or self.attempted:
            return None
        schema = {"camera": "biome.capture.v1", "sensors": "biome.sensor_tick.v1"}.get(self.role)
        if (schema is None or not isinstance(self.receiver_id, str) or not self.receiver_id or
                not isinstance(packet, dict) or packet.get("schema") != schema or
                packet.get("receiver_id") != self.receiver_id or packet.get("camera_id") != "rear"):
            return None

        # Consume this connection's one attempt even when the option is unavailable.
        # Both the helper and its callbacks run on the accepted socket's event loop.
        self.attempted = True
        result = {"role": self.role, "requested_tos": 0xa0,
                  "observed_monotonic_s": time.monotonic(), "status": "error"}
        stage = "socket"
        try:
            transport = connection.transport
            accepted = transport.get_extra_info("socket")
            if accepted is None:
                result["status"] = "socket_unavailable"
                return result
            if accepted.family != socket.AF_INET:
                result["status"] = "not_ipv4"
                return result
            # asyncio keeps these addresses in transport extra_info. Retain ports
            # for matching the subsequent passive trace, never headers or payload.
            for field, key in (("local_port", "sockname"), ("peer_port", "peername")):
                address = transport.get_extra_info(key)
                if isinstance(address, tuple) and len(address) >= 2 and type(address[1]) is int:
                    result[field] = address[1]
            stage = "get_before"
            result["previous_tos"] = accepted.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
            stage = "set"
            accepted.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xa0)
            stage = "get_after"
            result["readback_tos"] = accepted.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
            result["status"] = "applied" if result["readback_tos"] == 0xa0 else "readback_mismatch"
        except Exception as exc:
            # This optional experiment must not interrupt camera/sensor delivery.
            # Do not retain exception text, which may contain connection details.
            result["error_stage"] = stage
            result["error_type"] = type(exc).__name__
            if type(getattr(exc, "errno", None)) is int:
                result["error_errno"] = exc.errno
        return result
