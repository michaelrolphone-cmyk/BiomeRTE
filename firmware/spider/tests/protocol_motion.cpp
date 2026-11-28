#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <limits>

#include "control.h"
#include "motion_engine.h"

using namespace spider;

static void verifyWire() {
    assert(crc16(reinterpret_cast<const uint8_t*>("123456789"), 9) == 0x29b1);
    Decoder decoder;
    Message received{};
    const Message original{Kind::Walk, Action::Forward, 0x7e7d1100, 400};
    uint8_t bytes[maxWireSize];
    const size_t length = encode(original, bytes);
    assert(!decoder.feed(0xa5, received));
    for (size_t i = 0; i < length; ++i) {
        assert(decoder.feed(bytes[i], received) == (i == length - 1));
    }
    assert(received.kind == original.kind && received.action == original.action &&
           received.sequence == original.sequence && received.value == original.value);
    assert(!isCommand({Kind::Walk, Action::Zero, 1, 100}));
    assert(!isCommand({Kind::Walk, Action::Forward, 1, 1001}));
    assert(!isCommand({Kind::Stop, Action::Forward, 1, 0}));

    for (size_t i = 0; i < length; ++i) {
        assert(!decoder.feed(i == 2 ? bytes[i] ^ 1 : bytes[i], received));
    }
    assert(!decoder.feed(delimiter, received));
    for (int i = 0; i < 200; ++i) assert(!decoder.feed(7, received));
    for (size_t i = 0; i < length; ++i) {
        assert(decoder.feed(bytes[i], received) == (i == length - 1));
    }
}

static void verifyGaits() {
    const std::array<Action, 6> walks = {Action::Forward, Action::Backward, Action::ShiftLeft,
                                        Action::ShiftRight, Action::TurnLeft, Action::TurnRight};
    for (const auto action : walks) {
        MotionEngine motion;
        assert(motion.command({Kind::Walk, action, 1, 400}, 0));
        const Program expected = programFor(action);
        uint32_t now = 0;
        for (int repeat = 0; repeat < 2; ++repeat) {
            for (size_t step = 0; step < expected.length; ++step) {
                now += expected.frames[step].durationMs;
                // Repeating the same walk renews it and preserves the gait phase.
                assert(motion.command({Kind::Walk, action, 1, 400}, now));
                assert(std::memcmp(motion.angles(), expected.frames[step].angles, 8) == 0);
            }
        }
        motion.tick(now + linkTimeoutMs - 1);
        assert(motion.state() == State::Walking);
        motion.tick(now + linkTimeoutMs);
        assert(motion.state() == State::Returning);
        motion.tick(now + linkTimeoutMs + stand[0].durationMs);
        assert(motion.state() == State::Idle && motion.action() == Action::Stand);
        assert(std::memcmp(motion.angles(), stand[0].angles, 8) == 0);
    }
}

static void verifyStopsAndPoses() {
    MotionEngine motion;
    const auto wrap = std::numeric_limits<uint32_t>::max() - 50;
    assert(motion.command({Kind::Walk, Action::Forward, 1, 100}, wrap));
    assert(motion.command({Kind::Probe, Action::None, 2, 0}, 15));
    motion.tick(48);
    assert(motion.state() == State::Walking);
    motion.tick(49);
    assert(motion.state() == State::Returning);
    motion.tick(600);
    assert(motion.state() == State::Idle);

    assert(motion.command({Kind::Pose, Action::Sleep, 3, 0}, 1000));
    for (uint32_t now = 1100; now <= 3000; now += 100) {
        assert(motion.command({Kind::Probe, Action::None, now, 0}, now));
    }
    assert(motion.state() == State::Idle && motion.action() == Action::Sleep);
    assert(std::memcmp(motion.angles(), sleep[3].angles, 8) == 0);
    motion.tick(5000);
    assert(motion.action() == Action::Sleep); // A completed static pose is held.

    assert(motion.command({Kind::Walk, Action::TurnRight, 4, 400}, 5000));
    assert(motion.command({Kind::Stop, Action::None, 5, 0}, 5050));
    assert(motion.state() == State::Returning && motion.action() == Action::Stand);
    assert(motion.command({Kind::Stop, Action::None, 6, 0}, 5240));
    motion.tick(5550);
    assert(motion.state() == State::Idle); // A repeated stop can't restart centering.
    assert(motion.command({Kind::Pose, Action::Lie, 7, 0}, 5600));
    motion.tick(5850);
    assert(motion.state() == State::Returning);
}

static void verifyCommandLeases() {
    CommandGate gate;
    const Intent walk{Kind::Walk, Action::Forward, 1, 100, 400100};
    assert(!gate.accept(walk, 99));
    assert(!gate.accept(walk, 400100));
    assert(gate.accept(walk, 100));
    assert(!gate.accept(walk, 101));
    assert(!gate.accept({Kind::Walk, Action::Forward, 2, 100, 1000101}, 100));
    assert(!gate.accept({Kind::Walk, Action::Sleep, 2, 100, 200}, 100));
    assert(!gate.accept({Kind::Pose, Action::Forward, 2, 100, 200}, 100));
    assert(!gate.accept({Kind::Stop, Action::Lie, 2, 100, 200}, 100));
    assert(gate.accept({Kind::Stop, Action::None, 2, 100, 200}, 100));
    gate.reset();
    assert(gate.accept(walk, 101));
}

int main() {
    verifyWire();
    verifyGaits();
    verifyStopsAndPoses();
    verifyCommandLeases();
}
