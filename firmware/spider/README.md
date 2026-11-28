# LAFVIN quadruped firmware

The LAFVIN B0H699465L kit uses an ESP8266 and eight MG90 servos. The ESP8266 runs the factory motion tables under UART control. A separate ESP32-S3-CAM publishes camera, 8×8 ToF and BNO085 readings to the Rover socket and routes signed motion commands to the ESP8266.

## Build

Use PlatformIO from this directory.

```sh
pio run -e lafvin-motion
cp include/site_config.example.h include/site_config.h
# Fill in the camera's Wi-Fi and dedicated Rover ingress settings.
pio run -e spider-camera
```

The ESP8266 target uses the NodeMCU ESP8266 4 MB flash profile. The camera target uses the Freenove-compatible ESP32-S3-CAM/OV2640 pin map and octal PSRAM. Check the module markings and header pinout before flashing or wiring. An empty control secret leaves telemetry enabled and motion commands disabled. Use a distinct random control secret of at least 32 bytes and an ingress token of at least 32 bytes. Keep the configured header and its resulting binary private.

## Wiring

The spider camera uses the same ESP32-S3-CAM and sensor harness pinout as the Rover rear camera. The UART connects to the spider's ESP8266 motion controller.

| Connection | ESP32-S3-CAM GPIO | Other end |
| --- | ---: | --- |
| Motion UART TX | 38 | ESP8266 RX / GPIO3 |
| Motion UART RX | 39 | ESP8266 TX / GPIO1 |
| UART ground | GND | ESP8266 GND |
| ToF I²C SDA / SCL | 41 / 42 | Both VL53L5CX SDA / SCL |
| Forward / rearward ToF LPn | 1 / 21 | Enable pin on each VL53L5CX |
| BNO085 SPI SCK / MISO / MOSI | 14 / 47 / 2 | SCL / SDA / ADD |
| BNO085 SPI CS / INT / RST | 40 / 3 / 46 | CS / INT / RST |
| BNO085 VCC / GND | 3.3 V / GND | Power / common ground |

UART is 3.3 V logic, 115200 baud, 8N1. The LAFVIN kit uses the same RX/TX for the factory ultrasonic sensor and the onboard CP2102 USB bridge. Leave the factory ultrasonic sensor unplugged. Verify that the CP2102 TX cannot drive GPIO3 while the S3 TX is attached. Isolate the bridge TX if the board connects it directly. Disconnect the S3 UART leads for ESP8266 USB flashing and reconnect the bridge TX when flashing requires it. Use the kit's servo supply and an appropriately rated supply for the camera and sensors, with a shared ground.

On the shared I²C bus, the firmware enables the ToF heads individually and assigns the forward head address `0x2a` and the rearward head address `0x2b`. An optional TCA9548A at `0x70` can still select modules at `0x29` on channels 0 and 1. The driver preserves the native zone order. Set both BNO085 mode pins, PS0 and PS1, high for SPI. GPIO46 is also an ESP32-S3 boot strap and must be low during reset to enter serial download mode. Keep the SD slot empty. Debug logs use USB CDC.

The servo output order is GPIO14, 12, 13, 15, 16, 5, 4, 2. The motion target reads the factory EEPROM trim bytes at offsets 0–7 without writing flash. Use the factory calibration procedure with the legs supported before bench testing a gait. Flash a build with the same EEPROM layout and skip a full erase if you need to retain factory trim. A blank trim region uses zero trim. The boot pose is the factory calibration zero. The normal resting and stop pose is `stand`.

- Walking actions are `forward`, `backward`, `shift_left`, `shift_right`, `turn_left` and `turn_right`.
- Poses are `stand`, `lie`, `sleep` and calibration `zero`.
- A walk renews the current gait when the direction stays the same. On lease expiry, explicit stop or 250 ms without valid UART traffic during motion, the controller interrupts the gait and transitions to stand. A completed static pose is held until the next command.

The gait and pose values come from the vendor's `0.SpiderBot/actionarray.h` distributed with the [LAFVIN tutorial resources](https://lafvin-quadruped-spider-robot.readthedocs.io/en/latest/Introduction.html#resource-download). For original firmware recovery or calibration, see the [LAFVIN flash guide](https://lafvin-quadruped-spider-robot.readthedocs.io/en/latest/Programmingprogram.html).

## Rover socket

Point the camera at an ingress instance configured for the spider receiver ID and token. Use a dedicated instance for the spider, since the current Rover listener accepts a single front receiver ID. The firmware uses the existing Rover provider's two WebSocket paths with `Authorization: Sensor <token>`. The existing car's receiver ID can stay bound to its own instance.

| Path | Camera firmware sends | Peer sends |
| --- | --- | --- |
| `/camera` | Up to 5 Hz 1280×720 JPEG. Four big-endian bytes for metadata length, then `biome.capture.v1` JSON, then the JPEG. | Rover `clock`, `camera_ready` and `camera_ack` |
| `/sensors` | `biome.sensor_tick.v1` JSON with `lidar`, `bno085` and `spider_motion` | Rover `clock` and optional `spider_command.v1` |

The JPEG lane allows one frame awaiting acknowledgement and takes the latest frame from the camera driver. The sensor lane sends independent ticks with each reading's device-clock timestamp and freshness. Range grids carry 64 distances in millimetres and 64 ST target-status codes. BNO085 telemetry carries native-axis acceleration, gyro, magnetic field, linear acceleration and the game-rotation quaternion (WXYZ, relative yaw). It is available under `sensor_packet.bno085` and preserves the sensor frame. The Rover's existing MPU9250-specific attitude and mounting code needs a BNO085 consumer before using these readings for pose estimation.

To send motion, use the `/sensors` peer connection and wait for `spider_motion.enabled`, `spider_motion.uart_ready`, and the current `spider_motion.session`. Use Rover clock replies to express the command times in the camera's monotonic microseconds. The last `capture_monotonic_us` from a freshly received sensor tick is also a conservative issued time for a short bench command.

```json
{
  "type": "spider_command.v1",
  "receiver_id": "spider-camera",
  "session": "<from spider_motion>",
  "sequence": 1,
  "mode": "walk",
  "action": "forward",
  "issued_monotonic_us": 10000000,
  "expires_monotonic_us": 10400000,
  "signature": "<hex HMAC-SHA256>"
}
```

Sign UTF-8 fields joined by one LF in this exact order with no trailing LF.

```text
spider_command.v1
<session>
<receiver_id>
<sequence as decimal>
<mode>
<action>
<issued_monotonic_us as decimal>
<expires_monotonic_us as decimal>
```

Use the independent control secret as the HMAC key and lowercase hexadecimal for the signature. Send `mode=pose` with a pose name or `mode=stop` with `action=none`. Sequence numbers increase within each socket session. The lifetime may be at most 1,000 ms. Renew a held walk with a fresh sequence and deadline. A pose is a single accepted transition and can finish after its admission deadline. Opening a new socket invalidates the old session. Clock probes maintain the transport and cannot extend a walk.

Motion telemetry reports the last accepted socket sequence, the last sequence forwarded to UART and the last one acknowledged by the ESP8266. Status values are 0 idle, 1 walking, 2 posing and 3 returning to stand. An acknowledgement reports servo-command acceptance rather than measured joint motion. Rover's current server accepts the telemetry but does not generate these motion commands. The signed command schema is the firmware interface for the later Rover control integration.

## Verification

```sh
c++ -std=c++11 -Wall -Wextra -Werror -pedantic -I src tests/protocol_motion.cpp -o /tmp/spider-motion-test
/tmp/spider-motion-test
```

Before ground operation, support the chassis and verify the pinout and trim. With the Rover socket connected, check for complete JPEGs and valid, current ToF/IMU readings. Send a stand, walk renewal and stop over the sensor lane. Confirm that UART acknowledgement tracks the commands. Disconnect Wi-Fi and UART in separate trials during a supported walk and verify the return to stand.

For an isolated bench socket, install `websockets>=14` in the host's Python environment, set `SPIDER_ROVER_SENSOR_TOKEN` and run `python tests/rover_peer.py`. Point the camera at that host and port. The peer prints live JPEG, ToF, BNO085 and UART observations. With the chassis supported, set `SPIDER_ROVER_CONTROL_SECRET` and add `--action stand` to send one signed pose and check its UART acknowledgement. A walking action sends a single 400 ms lease.

## Joint tracking and servo-only odometry

The ESP8266 samples all eight **trimmed PWM targets** at 50 Hz and sends them with the actuator boot ID, uptime, motion state, and current keyframe. The S3 integrates these samples locally and publishes the latest state on the 20 Hz sensor lane under `spider_motion`. The stock servos do not expose their measured joint angle or load.

| Field | Meaning |
| --- | --- |
| `joints` | Eight transmitted targets in factory GPIO order, actuator uptime and boot, camera receipt time, and motion keyframe. Keyframe 255 means idle. |
| `legs_body_mm` | Hip, lift joint, and foot coordinates for each leg. The body frame starts at the chassis center with X forward, Y left, and Z up. |
| `servo_odometry` | Pose `x_mm`, `y_mm`, and `heading_rad` relative to the origin, integrated path length, straight-line displacement, assumed support masks, fit residual and continuity. Heading is positive counterclockwise from above. |

Copy `include/leg_calibration.example.h` to `include/leg_calibration.h` and enter the geometry of the assembled chassis before building the camera. Use the center of the chassis as the origin and mark which end is forward. For each leg, measure the hip pivot and both shaft-axis directions in the body frame. At the chosen reference PWM targets, measure the vector from the hip pivot to the lift shaft, then from that shaft to the rubber contact point. Record the reference **trimmed** targets and the sign of each servo rotation about its axis. Use a unit vector for each axis. A calibration revision identifies that linkage setup. The firmware sends raw targets before these measurements and withholds metric coordinates until the calibration is valid.

The estimator aligns the same two or more presumed planted feet between samples. It uses the lowest modeled feet as candidates unless a custom pose or optional calibrated factory phase supplies the stance mask. To define factory masks, use six rows in forward, backward, shift-left, shift-right, turn-left, turn-right order. Each row has eleven entries, one per target keyframe. The entry describes support during the transition **to** that keyframe. Use 255 for the height-based default and mask bits 1, 2, 4, and 8 for front right, rear right, front left, and rear left. The last three columns of each turn row are unused.

Too few shared feet, inconsistent fitted foot positions, gaps, and actuator reboots break continuity. Subsequent valid samples can resume accumulation, with `continuous_from_origin` remaining false until reset. The residual tests consistency among feet. It cannot detect feet that slide together, servo stalls, or actual ground contact. This is planar dead reckoning using commanded geometry. Confirm the geometry against foot marks with the robot supported, then compare repeated straight runs, side steps, in-place turns, and return-to-origin paths on the intended floor before relying on accumulated distance. The latest valid increment is also available in the preceding body frame under `latest_body_delta`. The Rover sensor lane can skip intermediate actuator samples, so use the cumulative pose, origin epoch and rejection count for interval comparison. Camera, LiDAR, and IMU readings remain available for a future correction layer.

Send the signed command `mode=origin, action=zero` to reset the odometry reference without actuating the servos. The bench peer accepts `--action zero_origin`. This is distinct from `mode=pose, action=zero`, which moves the legs to the assembly-calibration pose.

## Authoring poses

Add named poses to a JSON file like [poses.example.json](tests/poses.example.json). The eight angles are in factory GPIO order. Each command interpolates to the target in 400–5,000 ms and holds the completed position. The ESP8266 checks each axis against the existing factory gait and pose envelope. A pose can declare which feet are expected to support the body during the move and final hold. Omit `stance_mask` or use 0 when support is unknown. A manipulation routine can send successive poses to approach, reach and retract without adding firmware actions.

```sh
python tests/rover_peer.py --pose-file tests/poses.example.json --pose low_stance
python tests/rover_peer.py --action zero_origin
```

For a custom target set `mode=joint_pose` and `action=custom` on the sensor socket. Include `duration_ms`, `stance_mask`, and `angles_deg`. Append those same values to the signed message as three additional LF-separated lines in that order. Sign the duration and mask as decimal integers and the angles as eight comma-separated decimal integers with no spaces or trailing LF. New authored poses can be used without reflashing either board. Flash both updated targets once to enable the new joint reports and custom pose commands.

```sh
c++ -std=c++11 -Wall -Wextra -Werror -pedantic -I src tests/leg_odometry.cpp -o /tmp/spider-odometry-test
/tmp/spider-odometry-test
```
