#pragma once

#include "factory_keyframes.h"
#include "spider_uart.h"

namespace spider {

struct Program {
    const Keyframe* frames;
    size_t length;
};

template<size_t N> Program program(const Keyframe (&frames)[N]) { return {frames, N}; }

inline Program programFor(Action action) {
    switch (action) {
        case Action::Forward: return program(forward);
        case Action::Backward: return program(backward);
        case Action::ShiftLeft: return program(shiftLeft);
        case Action::ShiftRight: return program(shiftRight);
        case Action::TurnLeft: return program(turnLeft);
        case Action::TurnRight: return program(turnRight);
        case Action::Stand: return program(stand);
        case Action::Lie: return program(lie);
        case Action::Sleep: return program(sleep);
        case Action::Zero: return program(zero);
        default: return {nullptr, 0};
    }
}

class MotionEngine {
public:
    MotionEngine() { memcpy(angles_, zero[0].angles, sizeof(angles_)); }

    const uint8_t* angles() const { return angles_; }
    State state() const { return state_; }
    Action action() const { return action_; }
    uint8_t stance() const { return stance_; }
    uint8_t keyframe() const { return active_.length ? frame_ : 255; }

    bool command(const Message& message, uint32_t now) {
        if (!isCommand(message)) return false;
        tick(now);
        lastLink_ = now;
        switch (message.kind) {
            case Kind::Walk:
                walkStart_ = now;
                walkLease_ = message.value;
                if (state_ != State::Walking || action_ != message.action) {
                    start(message.action, State::Walking, now);
                }
                break;
            case Kind::Pose:
                if (action_ != message.action || (state_ != State::Idle && state_ != State::Posing)) {
                    start(message.action, State::Posing, now);
                }
                break;
            case Kind::JointPose:
                memcpy(custom_.angles, message.angles, jointCount);
                custom_.durationMs = message.value;
                start(Action::Custom, State::Posing, now);
                stance_ = message.stance;
                break;
            case Kind::Stop:
                stop(now);
                break;
            default:
                break;
        }
        return true;
    }

    void tick(uint32_t now) {
        if (!active_.length) return;
        const bool needsLink = state_ == State::Walking || state_ == State::Posing;
        if ((needsLink && now - lastLink_ >= linkTimeoutMs) ||
            (state_ == State::Walking && now - walkStart_ >= walkLease_)) {
            stop(now);
        }
        const auto& key = active_.frames[frame_];
        const uint32_t elapsed = now - started_;
        if (elapsed < key.durationMs) {
            for (size_t i = 0; i < sizeof(angles_); ++i) {
                const int delta = int(key.angles[i]) - origin_[i];
                angles_[i] = origin_[i] + delta * int(elapsed) / key.durationMs;
            }
            return;
        }
        memcpy(angles_, key.angles, sizeof(angles_));
        if (++frame_ == active_.length) {
            if (state_ != State::Walking) {
                active_ = {nullptr, 0};
                state_ = State::Idle;
                return;
            }
            frame_ = 0;
        }
        beginFrame(now);
    }

private:
    void beginFrame(uint32_t now) {
        memcpy(origin_, angles_, sizeof(angles_));
        started_ = now;
    }

    void start(Action action, State state, uint32_t now) {
        action_ = action;
        state_ = state;
        active_ = action == Action::Custom ? Program{&custom_, 1} : programFor(action);
        stance_ = inferStance;
        frame_ = 0;
        beginFrame(now);
    }

    void stop(uint32_t now) {
        if (state_ != State::Returning && (state_ != State::Idle || action_ != Action::Stand)) {
            start(Action::Stand, State::Returning, now);
        }
    }

    Keyframe custom_{};
    uint8_t stance_ = inferStance;
    uint8_t angles_[8] = {};
    uint8_t origin_[8] = {};
    Program active_{nullptr, 0};
    size_t frame_ = 0;
    uint32_t started_ = 0;
    uint32_t lastLink_ = 0;
    uint32_t walkStart_ = 0;
    uint16_t walkLease_ = 0;
    State state_ = State::Idle;
    Action action_ = Action::Zero;
};

}  // namespace spider
