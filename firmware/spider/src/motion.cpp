#include <Arduino.h>
#include <EEPROM.h>
#include <ESP8266WiFi.h>
#include <Servo.h>

#include "motion_engine.h"

namespace {

constexpr uint8_t servoPins[] = {14, 12, 13, 15, 16, 5, 4, 2};
constexpr int pulseMinUs = 400;
constexpr int pulseMaxUs = 2400;
constexpr uint32_t updateMs = 10;
Servo servos[8];
int8_t trims[8] = {};
spider::MotionEngine motion;
spider::Decoder decoder;
uint32_t lastWrite;

void writeServo(size_t channel) {
    const int angle = constrain(int(motion.angles()[channel]) + trims[channel], 1, 180);
    servos[channel].writeMicroseconds(map(angle, 1, 180, pulseMinUs, pulseMaxUs));
}

void acknowledge(uint32_t sequence) {
    uint8_t bytes[spider::maxWireSize];
    const auto size = spider::encode(
        {spider::Kind::Status, motion.action(), sequence, uint16_t(motion.state())}, bytes);
    if (Serial.availableForWrite() >= int(size)) Serial.write(bytes, size);
}

}  // namespace

void setup() {
    WiFi.mode(WIFI_OFF);
    Serial.begin(spider::uartBaud);
    EEPROM.begin(512);  // Same offsets 0–7 as the factory sketch. Never commit here.
    bool erased = true;
    for (size_t i = 0; i < sizeof(trims); ++i) {
        trims[i] = static_cast<int8_t>(EEPROM.read(i));
        erased &= trims[i] == -1;
    }
    for (size_t i = 0; i < sizeof(trims); ++i) {
        if (erased || trims[i] < -45 || trims[i] > 45) trims[i] = 0;
        servos[i].attach(servoPins[i], pulseMinUs, pulseMaxUs);
        writeServo(i);
    }
}

void loop() {
    const uint32_t now = millis();
    motion.tick(now);
    for (int count = 0; count < 64 && Serial.available(); ++count) {
        spider::Message command{};
        if (decoder.feed(Serial.read(), command) && motion.command(command, millis())) {
            acknowledge(command.sequence);
        }
    }
    if (now - lastWrite >= updateMs) {
        lastWrite = now;
        for (size_t i = 0; i < sizeof(trims); ++i) writeServo(i);
    }
    yield();
}
