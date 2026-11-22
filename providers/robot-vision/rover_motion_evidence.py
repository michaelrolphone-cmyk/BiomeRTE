"""Fresh mapper displacement evidence for the shared ToF rover estimator."""
import math
import time

def motion_evidence(frame, localization, wall=None, monotonic=None):
    wall=time.time() if wall is None else wall
    monotonic=time.monotonic() if monotonic is None else monotonic
    received=frame.get("receive_decode_monotonic_s")
    fresh=isinstance(received,(int,float)) and 0<=monotonic-received<=3
    pose=localization.get("T_map_camera")
    tracking=bool(localization.get("valid")) and fresh
    try:
        point=[float(pose[i][3]) for i in range(3)]
        tracking=tracking and all(math.isfinite(v) for v in point)
    except (TypeError,IndexError,ValueError,KeyError):
        tracking=False;point=None
    return {"schema":"rover.motion.evidence/1","observed_at":wall-(monotonic-received) if fresh else wall,
            "tracking":tracking,"map_id":localization.get("map_id"),
            "position":point if tracking else None,"metric":localization.get("units")=="m",
            "frame_id":frame.get("frame_id")}
