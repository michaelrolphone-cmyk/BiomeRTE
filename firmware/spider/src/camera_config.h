#pragma once

#if __has_include("site_config.h")
#include "site_config.h"
#else
#include "site_config.example.h"
#endif

namespace spider {

constexpr char receiverId[] = SPIDER_RECEIVER_ID;
// Rover assigns the front role inside this robot's own ingress instance.
constexpr char cameraRole[] = "front";

namespace pins {
// Freenove / compatible ESP32-S3-CAM with the OV2640 camera connector.
constexpr int cameraData[] = {11, 9, 8, 10, 12, 18, 17, 16};
constexpr int xclk = 15, pclk = 13, vsync = 6, href = 7, siod = 4, sioc = 5;
constexpr int uartRx = 41, uartTx = 1;
constexpr int tofSda = 38, tofScl = 39;
constexpr int imuSck = 14, imuMiso = 47, imuMosi = 2, imuCs = 40, imuInt = 21, imuReset = 42;
}  // namespace pins

}  // namespace spider
