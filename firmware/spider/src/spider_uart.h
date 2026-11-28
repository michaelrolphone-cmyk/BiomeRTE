#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace spider {

constexpr uint32_t uartBaud = 115200;
constexpr uint16_t linkTimeoutMs = 250;
constexpr uint16_t maximumWalkMs = 1000;

enum class Kind : uint8_t { Probe = 1, Walk, Pose, Stop, Status, JointPose, JointSample, Origin };
enum class Action : uint8_t {
    None, Forward, Backward, ShiftLeft, ShiftRight, TurnLeft, TurnRight, Stand, Lie, Sleep, Zero, Custom
};
enum class State : uint8_t { Idle, Walking, Posing, Returning };

struct ActionName {
    Action action;
    const char* name;
};

constexpr ActionName actions[] = {
    {Action::Forward, "forward"}, {Action::Backward, "backward"},
    {Action::ShiftLeft, "shift_left"}, {Action::ShiftRight, "shift_right"},
    {Action::TurnLeft, "turn_left"}, {Action::TurnRight, "turn_right"},
    {Action::Stand, "stand"}, {Action::Lie, "lie"}, {Action::Sleep, "sleep"}, {Action::Zero, "zero"}, {Action::Custom, "custom"},
};

inline bool isWalk(Action action) { return action >= Action::Forward && action <= Action::TurnRight; }
inline bool isPose(Action action) { return action >= Action::Stand && action <= Action::Zero; }

constexpr uint8_t inferStance = 255;
constexpr size_t jointCount = 8;
constexpr uint16_t jointPeriodMs = 20;

struct Message {
    Message(Kind kind = {}, Action action = {}, uint32_t sequence = 0, uint16_t value = 0)
        : kind(kind), action(action), sequence(sequence), value(value) {}
    Kind kind;
    Action action;
    uint32_t sequence;
    uint16_t value; // Walk lease, custom pose duration, status state, or sample state/keyframe.
    uint8_t angles[jointCount] = {};
    uint8_t stance = inferStance;
    uint32_t boot = 0; // JointSample only. Its sequence is actuator uptime in ms.
};

inline bool validJointPose(const Message& message) {
    constexpr uint8_t minimum[jointCount] = {30, 39, 45, 45, 45, 45, 45, 30};
    constexpr uint8_t maximum[jointCount] = {135, 135, 141, 150, 150, 135, 135, 135};
    if (message.action != Action::Custom || message.value < 400 || message.value > 5000 ||
        message.stance > 15 || message.boot) return false;
    for (size_t i = 0; i < jointCount; ++i) {
        if (message.angles[i] < minimum[i] || message.angles[i] > maximum[i]) return false;
    }
    return true;
}

inline bool isCommand(const Message& message) {
    switch (message.kind) {
        case Kind::Walk:
            return isWalk(message.action) && message.value > 0 && message.value <= maximumWalkMs;
        case Kind::Pose:
            return isPose(message.action) && message.value == 0;
        case Kind::JointPose:
            return validJointPose(message);
        case Kind::Probe:
        case Kind::Stop:
            return message.action == Action::None && message.value == 0;
        default:
            return false;
    }
}

inline uint16_t crc16(const uint8_t* data, size_t size) {
    uint16_t crc = 0xffff;
    for (size_t i = 0; i < size; ++i) {
        crc ^= static_cast<uint16_t>(data[i]) << 8;
        for (int bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : crc << 1;
        }
    }
    return crc;
}

constexpr uint8_t delimiter = 0x7e;
constexpr uint8_t escape = 0x7d;
constexpr uint8_t escapeMask = 0x20;
constexpr size_t packetSize = 11;
constexpr size_t extendedSize = packetSize + jointCount + 5;
constexpr size_t maxWireSize = 2 * extendedSize + 2;

inline size_t sizeFor(Kind kind) {
    return kind == Kind::JointPose || kind == Kind::JointSample ? extendedSize : packetSize;
}

inline size_t encode(const Message& message, uint8_t (&wire)[maxWireSize]) {
    uint8_t bytes[extendedSize] = {1, uint8_t(message.kind), uint8_t(message.action)};
    for (int i = 0; i < 4; ++i) bytes[3 + i] = message.sequence >> (8 * i);
    bytes[7] = message.value;
    bytes[8] = message.value >> 8;
    const size_t size = sizeFor(message.kind);
    if (size == extendedSize) {
        memcpy(bytes + 9, message.angles, jointCount);
        bytes[17] = message.stance;
        for (int i = 0; i < 4; ++i) bytes[18 + i] = message.boot >> (8 * i);
    }
    const uint16_t crc = crc16(bytes, size - 2);
    bytes[size - 2] = crc;
    bytes[size - 1] = crc >> 8;
    size_t length = 0;
    wire[length++] = delimiter;
    for (size_t i = 0; i < size; ++i) {
        uint8_t byte = bytes[i];
        if (byte == delimiter || byte == escape) {
            wire[length++] = escape;
            byte ^= escapeMask;
        }
        wire[length++] = byte;
    }
    wire[length++] = delimiter;
    return length;
}

class Decoder {
public:
    bool feed(uint8_t byte, Message& message) {
        if (byte == delimiter) {
            const size_t size = length_;
            const bool complete = started_ && !escaped_ && size >= packetSize &&
                size == sizeFor(Kind(bytes_[1]));
            started_ = true;
            escaped_ = false;
            length_ = 0;
            if (!complete || bytes_[0] != 1 ||
                crc16(bytes_, size - 2) != (bytes_[size - 2] | uint16_t(bytes_[size - 1]) << 8)) return false;
            uint32_t sequence = 0;
            for (int i = 0; i < 4; ++i) sequence |= uint32_t(bytes_[3 + i]) << (8 * i);
            message = {Kind(bytes_[1]), Action(bytes_[2]), sequence,
                       uint16_t(bytes_[7] | uint16_t(bytes_[8]) << 8)};
            if (size == extendedSize) {
                memcpy(message.angles, bytes_ + 9, jointCount);
                message.stance = bytes_[17];
                for (int i = 0; i < 4; ++i) message.boot |= uint32_t(bytes_[18 + i]) << (8 * i);
            }
            return true;
        }
        if (!started_) return false;
        if (escaped_) {
            byte ^= escapeMask;
            escaped_ = false;
            if (byte != delimiter && byte != escape) {
                started_ = false;
                return false;
            }
        } else if (byte == escape) {
            escaped_ = true;
            return false;
        }
        if (length_ == extendedSize) {
            started_ = false;
            return false;
        }
        bytes_[length_++] = byte;
        return false;
    }

private:
    uint8_t bytes_[extendedSize] = {};
    size_t length_ = 0;
    bool started_ = false;
    bool escaped_ = false;
};

}  // namespace spider
