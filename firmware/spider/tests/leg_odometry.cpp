#include <cassert>
#include <cstring>

#include "control.h"
#include "leg_odometry.h"
#include "motion_engine.h"

using namespace spider;

static void near(float observed, float expected) {
    assert(fabsf(observed - expected) < .001f);
}

static LegCalibration fixtureCalibration() {
    const LegModel model{{30, 20, 0}, {0, 0, 1}, {35, 0, 0}, {0, 1, 0},
                         {20, 0, -25}, 90, 90, 1, 1};
    LegCalibration calibration{"test-v1", {model, model, model, model}, 5, 2, nullptr};
    calibration.legs[1].hipMm.x = -30;
    calibration.legs[2].hipMm.y = -20;
    calibration.legs[3].hipMm.x = -30;
    calibration.legs[3].hipMm.y = -20;
    return calibration;
}

static void testKinematics() {
    LegCalibration calibration = fixtureCalibration();
    assert(validCalibration(calibration));
    const auto points = legPoints(calibration.legs[0], 180, 180);
    near(points.hip.x, 30);
    near(points.lift.y, 55);
    near(points.foot.x, 30);
    near(points.foot.y, 30);
    near(points.foot.z, -20);

    calibration.legs[0].liftAxis.x = 1;
    assert(!validCalibration(calibration));
}

static void testPlanarFit() {
    LegPoints previous[legCount]{}, current[legCount]{};
    const Vec3 corners[legCount] = {{60, 40, -50}, {-60, 40, -50},
                                   {60, -40, -50}, {-60, -40, -50}};
    const Vec3 shift{14, -9, -2};
    for (size_t i = 0; i < legCount; ++i) {
        previous[i].foot = corners[i];
        current[i].foot = rotate(corners[i] - shift, {0, 0, 1}, -.35f);
    }
    const auto result = fitStance(previous, current, 14, 2);
    assert(result.tracking == Tracking::Estimated);
    near(result.xMm, shift.x);
    near(result.yMm, shift.y);
    near(result.yawRad, .35f);
    near(result.residualMm, 0);
    assert(fitStance(previous, current, 1, 2).tracking == Tracking::NoSupport);

    current[3].foot.x += 20;
    assert(fitStance(previous, current, 15, 2).tracking == Tracking::Inconsistent);
    current[1].foot = current[0].foot;
    assert(fitStance(previous, current, 3, 2).tracking == Tracking::NoSupport);
}

static void testTrackingContinuity() {
    const auto calibration = fixtureCalibration();
    LegTracker tracker(calibration);
    Message sample{Kind::JointSample, Action::Stand, 20, 0};
    memset(sample.angles, 90, jointCount);
    sample.boot = 7;
    tracker.sample(sample, 21000);
    assert(tracker.state().tracking == Tracking::Reference && tracker.state().continuous);
    sample.sequence += 20;
    tracker.sample(sample, 41000);
    assert(tracker.state().tracking == Tracking::Estimated && tracker.state().support == 15);
    near(tracker.state().travelMm, 0);

    sample.sequence += 20;
    sample.stance = 1;
    tracker.sample(sample, 61000);
    assert(tracker.state().tracking == Tracking::NoSupport && !tracker.state().continuous);
    sample.stance = 15;
    sample.sequence += 100;
    tracker.sample(sample, 161000);
    assert(tracker.state().tracking == Tracking::Gap && tracker.state().rejected == 2);
    sample.sequence += 20;
    tracker.sample(sample, 181000);
    assert(tracker.state().tracking == Tracking::Estimated && !tracker.state().continuous);
    sample.boot += 1;
    sample.sequence = 20;
    tracker.sample(sample, 201000);
    assert(tracker.state().tracking == Tracking::Gap && !tracker.state().continuous);
    tracker.zero();
    assert(tracker.state().epoch == 1 && tracker.state().rejected == 0);
    tracker.sample(sample, 221000);
    assert(tracker.state().tracking == Tracking::Reference && tracker.state().continuous);

    const LegCalibration blank{};
    LegTracker uncalibrated(blank);
    uncalibrated.sample(sample, 10000);
    assert(uncalibrated.state().tracking == Tracking::Uncalibrated);
}

static void testDistanceAccumulation() {
    const auto calibration = fixtureCalibration();
    LegTracker tracker(calibration);
    Message sample{Kind::JointSample, Action::Forward, 20, uint16_t(State::Walking)};
    memset(sample.angles, 90, jointCount);
    sample.stance = 15;
    tracker.sample(sample, 20000);
    const auto initial = tracker.state().legs[0].foot;
    float expectedTravel = 0;
    constexpr uint8_t trajectory[] = {92, 94, 92, 90};
    for (auto angle : trajectory) {
        const auto before = tracker.state().legs[0].foot;
        for (const auto channel : hipChannel) sample.angles[channel] = angle;
        sample.sequence += 20;
        tracker.sample(sample, int64_t(sample.sequence) * 1000);
        const auto after = tracker.state().legs[0].foot;
        expectedTravel += hypotf(after.x - before.x, after.y - before.y);
        assert(tracker.state().tracking == Tracking::Estimated && tracker.state().continuous);
        near(tracker.state().xMm, initial.x - after.x);
        near(tracker.state().yMm, initial.y - after.y);
        near(tracker.state().travelMm, expectedTravel);
        near(tracker.state().yawRad, 0);
    }
    assert(tracker.state().travelMm > 0); // Returned to the origin, but the path has length.
    near(tracker.state().xMm, 0);
    near(tracker.state().yMm, 0);
}

static void testCalibratedSupport() {
    uint8_t plan[gaitCount][gaitFrames];
    memset(plan, inferStance, sizeof(plan));
    plan[0][1] = 6;
    auto calibration = fixtureCalibration();
    calibration.gaitStance = plan;
    LegTracker tracker(calibration);

    Message sample{Kind::JointSample, Action::Forward, 20,
                   uint16_t(256 | uint16_t(State::Walking))};
    memset(sample.angles, 90, jointCount);
    tracker.sample(sample, 20000);
    assert(tracker.state().support == 6 && tracker.state().supportSource == SupportSource::FactoryPhase);

    sample.stance = 3;
    sample.sequence = 40;
    tracker.sample(sample, 40000);
    assert(tracker.state().support == 3 && tracker.state().supportSource == SupportSource::CustomPose);
}

static void testCustomPose() {
    Message command{Kind::JointPose, Action::Custom, 0x7d7e7d7e, 500};
    uint8_t angles[] = {85, 90, 91, 92, 115, 95, 96, 50};
    memcpy(command.angles, angles, jointCount);
    command.stance = 12;
    assert(validJointPose(command));
    uint8_t wire[maxWireSize];
    const auto length = encode(command, wire);
    Decoder decoder;
    Message decoded;
    for (size_t i = 0; i < length; ++i) {
        assert(decoder.feed(wire[i], decoded) == (i == length - 1));
    }
    assert(decoded.kind == Kind::JointPose && decoded.sequence == command.sequence &&
           decoded.value == 500 && decoded.stance == 12);
    assert(memcmp(decoded.angles, angles, jointCount) == 0);
    for (size_t i = 0; i < length; ++i) {
        assert(!decoder.feed(i == 12 ? wire[i] ^ 1 : wire[i], decoded));
    }

    MotionEngine motion;
    assert(motion.command(decoded, 1000) && motion.stance() == 12 && motion.action() == Action::Custom);
    for (uint32_t now = 1100; now <= 1500; now += 100) {
        assert(motion.command({Kind::Probe, Action::None}, now));
    }
    assert(motion.state() == State::Idle && memcmp(motion.angles(), angles, jointCount) == 0);
    assert(motion.command({Kind::Stop, Action::None}, 1510));
    assert(motion.stance() == inferStance && motion.state() == State::Returning);

    CommandGate gate;
    Intent intent{Kind::JointPose, Action::Custom, 1, 100, 500000};
    intent.pose = command;
    assert(gate.accept(intent, 200));
    assert(!gate.accept(intent, 200));
    intent.sequence = 2;
    intent.pose.angles[7] = 20;
    assert(!gate.accept(intent, 200));
    assert(gate.accept({Kind::Origin, Action::Zero, 2, 100, 500000}, 200));

    command.kind = Kind::JointSample;
    command.boot = 0x12347d7e;
    const auto sampleLength = encode(command, wire);
    for (size_t i = 0; i < sampleLength; ++i) {
        assert(decoder.feed(wire[i], decoded) == (i == sampleLength - 1));
    }
    assert(decoded.kind == Kind::JointSample && decoded.boot == command.boot);
}

int main() {
    testKinematics();
    testPlanarFit();
    testTrackingContinuity();
    testDistanceAccumulation();
    testCalibratedSupport();
    testCustomPose();
}
