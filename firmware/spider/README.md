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

| Connection | ESP32-S3-CAM GPIO | Other end |
| --- | ---: | --- |
| Motion UART TX | 1 | ESP8266 RX / GPIO3 |
| Motion UART RX | 41 | ESP8266 TX / GPIO1 |
| UART ground | GND | ESP8266 GND |
| ToF I²C SDA / SCL | 38 / 39 | VL53L5CX SDA / SCL, or a TCA9548A at 0x70 |
| BNO085 SPI SCK / MISO / MOSI | 14 / 47 / 2 | SCL / SDA / ADD |
| BNO085 SPI CS / INT / RST | 40 / 21 / 42 | CS / INT / RST |

UART is 3.3 V logic, 115200 baud, 8N1. The LAFVIN kit uses the same RX/TX for the factory ultrasonic sensor and the onboard CP2102 USB bridge. Leave the factory ultrasonic sensor unplugged. Verify that the CP2102 TX cannot drive GPIO3 while the S3 TX is attached. Isolate the bridge TX if the board connects it directly. Disconnect the S3 UART leads for ESP8266 USB flashing and reconnect the bridge TX when flashing requires it. Use the kit's servo supply and an appropriately rated supply for the camera and sensors, with a shared ground.

The ToF driver accepts a single direct VL53L5CX at 0x29 or two modules at 0x29 on TCA9548A channels 0 and 1. It preserves the native zone order. Select SPI mode on the BNO085 breakout and wire its power and ground according to the breakout's pinout. The camera profile reserves the listed SPI and ToF pins, which overlap the SD interface on this board. It uses USB CDC for logs.

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
