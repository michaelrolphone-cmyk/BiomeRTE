#pragma once

#include <ArduinoJson.h>

namespace spider {

bool startSensors();
void appendSensors(JsonDocument& packet, int64_t tickUs);

}  // namespace spider
