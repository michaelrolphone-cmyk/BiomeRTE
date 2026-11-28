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
uint32_t lastSample;
uint32_t boot;
uint8_t targets[spider::jointCount];

void writeServo(size_t channel) {
    const int angle = constrain(int(motion.angles()[channel]) + trims[channel], 1, 180);
    targets[channel] = angle;
    servos[channel].writeMicroseconds(map(angle, 1, 180, pulseMinUs, pulseMaxUs));
}

void send(const spider::Message& message) {
    uint8_t bytes[spider::maxWireSize];
    const auto size = spider::encode(message, bytes);
    if (Serial.availableForWrite() >= int(size)) Serial.write(bytes, size);
}

}  // namespace

void setup() {
    boot = RANDOM_REG32;
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
    for (int count = 0; count < 64 && Serial.available(); ++count) {
        spider::Message command{};
        if (decoder.feed(Serial.read(), command) && motion.command(command, millis())) {
            send({spider::Kind::Status, motion.action(), command.sequence, uint16_t(motion.state())});
        }
    }
    const uint32_t now = millis();
    motion.tick(now);
    if (now - lastWrite >= updateMs) {
        lastWrite = now;
        for (size_t i = 0; i < sizeof(trims); ++i) writeServo(i);
        if (now - lastSample >= spider::jointPeriodMs) {
            lastSample = now;
            spider::Message sample{spider::Kind::JointSample, motion.action(), now,
                uint16_t(uint16_t(motion.keyframe()) << 8 | uint16_t(motion.state()))};
            memcpy(sample.angles, targets, sizeof(targets));
            sample.stance = motion.stance();
            sample.boot = boot;
            send(sample);
        }
    }
    yield();
}
