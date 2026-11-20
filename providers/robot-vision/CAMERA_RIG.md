# Front/rear camera rig: one shared map
The existing VisionService, VisionStore, map_id, native pySLAM Map, keyframe
IDs, landmark IDs, local mapping, loop database and checkpoints remain shared.
Rear feature extraction, descriptor matching, PnP and triangulation run on
slam-rear-tracking, independently of the front processing thread. Native graph
admission uses the existing backend/map locks; it does not append rear images
to the front tracker's temporal frame queue or initialize a second engine.

camera-rig.json in the vision state directory is hot-reloaded. The default rear
entry is disabled until a camera URL is configured. Set rear.camera.url to the
rear camera's RTSP endpoint and synchronized_capture=true to use /api/capture.
Supply independent validated rear camera intrinsics. A missing camera, missing
UART, missing LiDAR or unqualified intrinsics does not stop front processing or
rear raw-frame delivery.

T_front_camera_rear_camera_m maps rear optical coordinates into front optical
coordinates. Default rotation diag(-1,1,-1) faces180degrees backward; translation
[0,0,-0.0762] is three inches behind the FRONT OPTICAL CENTER at the same height.
The measurement is provisional. Change the matrix and increment revision after
measuring; no firmware update is necessary. Updating a mount clears the rear
temporal pair before any further triangulation.

Each device retains capture clock and boot identity. HTTP capture timing intervals
fit both clocks onto the same host monotonic timeline with explicit uncertainty.
Boot changes discard clock history. The rear firmware also offers timestamped
UART exchanges; UART loss does not gate camera SLAM. This is timestamp alignment,
not simultaneous shutter triggering. Reject pairs exceeding configured uncertainty
or pose gaps; never assume receipt time is exposure time.

Rear PnP matches the existing native map. New rear landmarks are triangulated
between rear views at synchronized rig poses and inserted directly into that
same native map. The metre-valued baseline requires the existing qualified
metric scale; without it, capture continues and mapping reports the exact reason.
The same native local mapper and loop closer process admitted rear keyframes.
Rear camera identities, calibration and rig anchor records persist in the backend
checkpoint. Front native temporal frames remain front-only.

Read rear images with robot.vision.frame inputs.camera_id=rear; normal queries
remain front-facing. robot.vision.rig.status and robot.vision.rig.configure expose
status/hot configuration. Source board firmware has no hard-coded physical mount.

Qualification still requires real rear-camera calibration, clock jitter under load,
rear-only/reverse motion, native loop closure with both views, and unplug/reconnect
tests with the purchased boards and sensors.
