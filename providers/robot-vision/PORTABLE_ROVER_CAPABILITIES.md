# Portable rover capabilities

Published contracts: robot.body.model:1, robot.control.model:1, robot.sonar.snapshot:1, robot.metric.status:1, robot.camera.exposure.status:1 and robot.camera.exposure.set:1.

Ground projection uses robot.vision.project_ground:2 without a caller floor-contact assertion. Version 1 keeps its executable schema for existing clients. Ray intersections assume the floor plane; they do not classify a selected object as floor contact.

Relay wiring on rover relay6ch.30eda0d95f24 is channel 1 forward, 2 back/reverse, 3 left, 4 right; 5/6 unassigned. Relays are binary on/off with no PWM. Speed versus continuous on-duration is an uncalibrated curve. Hold rear steering and throttle together.

The body model publishes owner-reported body dimensions, wheelbase, derived wheel track, camera pose, photo-measured sonar poses and the rigid AHRS mount 4 mm directly behind the camera. Measured sonar landmarks, height/parallax assumptions and provenance are retained in rover-mount-measurements.json. Body axes are forward/left/up.

Sonar ranges are metres. Each sensor has its own validity, freshness, age and source identity; the firmware trigger interval defines the nominal four-slot revisit rate, separate from host polling. Readings are sequential. Old readings are never relabelled fresh by a new HTTP response.

Metric geometry and sonar do not establish a monocular map scale. The map-specific applied attachment is exposed separately, including any unavailable reason. No numeric SLAM scale is invented.

Exposure and gain can be automatic or explicitly set through the authenticated camera API. Driver exposure values are not microseconds. The first-DMA capture timestamp is not calibrated exposure start, midpoint, end or rolling-shutter row timing; physical exposure uncertainty remains unknown. The onboard robot.camera.light.set:1 LED illuminates the camera view for driving.

The exposure firmware retains synchronized sonar range history from e3c0add and all existing camera/AHRS/RF synchronization.
