#pragma once

#include <math.h>

#include "spider_uart.h"

namespace spider {

constexpr size_t legCount = 4;
constexpr size_t gaitCount = 6;
constexpr size_t gaitFrames = 11;

struct Vec3 {
    float x, y, z;
};

inline Vec3 operator+(Vec3 a, Vec3 b) { return {a.x + b.x, a.y + b.y, a.z + b.z}; }
inline Vec3 operator-(Vec3 a, Vec3 b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline Vec3 operator*(Vec3 v, float s) { return {v.x * s, v.y * s, v.z * s}; }
inline float dot(Vec3 a, Vec3 b) { return a.x * b.x + a.y * b.y + a.z * b.z; }

inline Vec3 rotate(Vec3 v, Vec3 axis, float radians) {
    const float c = cosf(radians), s = sinf(radians);
    const Vec3 cross{axis.y * v.z - axis.z * v.y, axis.z * v.x - axis.x * v.z,
                     axis.x * v.y - axis.y * v.x};
    return v * c + cross * s + axis * (dot(axis, v) * (1 - c));
}

struct LegModel {
    Vec3 hipMm;           // Body origin to hip output shaft.
    Vec3 hipAxis;
    Vec3 hipToLiftMm;     // From hip shaft to lift shaft at both reference PWM angles.
    Vec3 liftAxis;
    Vec3 liftToFootMm;    // From lift shaft to ground-contact point at reference.
    float hipReferenceDeg, liftReferenceDeg;
    int hipSign, liftSign;
};

struct LegCalibration {
    const char* revision;
    LegModel legs[legCount]; // Front right, rear right, front left, rear left.
    float supportBandMm;
    float residualLimitMm;
    const uint8_t (*gaitStance)[gaitFrames]; // Optional six rows in Action order.
};

struct LegPoints {
    Vec3 hip, lift, foot;
};

constexpr size_t hipChannel[legCount] = {1, 2, 5, 6};
constexpr size_t liftChannel[legCount] = {0, 3, 4, 7};
constexpr float pi = 3.14159265358979323846f;

inline LegPoints legPoints(const LegModel& model, uint8_t hip, uint8_t lift) {
    const float yaw = model.hipSign * (hip - model.hipReferenceDeg) * (pi / 180);
    const float pitch = model.liftSign * (lift - model.liftReferenceDeg) * (pi / 180);
    const auto hinge = rotate(model.hipToLiftMm, model.hipAxis, yaw);
    const auto toe = rotate(rotate(model.liftToFootMm, model.liftAxis, pitch), model.hipAxis, yaw);
    return {model.hipMm, model.hipMm + hinge, model.hipMm + hinge + toe};
}

inline bool validCalibration(const LegCalibration& model) {
    if (!model.revision || !model.revision[0] || !isfinite(model.supportBandMm) ||
        !isfinite(model.residualLimitMm) || model.supportBandMm <= 0 || model.residualLimitMm <= 0) {
        return false;
    }
    for (const auto& leg : model.legs) {
        const float hipAxis2 = dot(leg.hipAxis, leg.hipAxis);
        const float liftAxis2 = dot(leg.liftAxis, leg.liftAxis);
        const float lowerLink2 = dot(leg.liftToFootMm, leg.liftToFootMm);
        if (!isfinite(hipAxis2) || fabsf(hipAxis2 - 1) > .001f ||
            !isfinite(liftAxis2) || fabsf(liftAxis2 - 1) > .001f ||
            !isfinite(dot(leg.hipMm, leg.hipMm)) || !isfinite(dot(leg.hipToLiftMm, leg.hipToLiftMm)) ||
            !isfinite(lowerLink2) || lowerLink2 <= 0 ||
            fabsf(float(leg.hipSign)) != 1 || fabsf(float(leg.liftSign)) != 1 ||
            !(leg.hipReferenceDeg >= 1 && leg.hipReferenceDeg <= 180) ||
            !(leg.liftReferenceDeg >= 1 && leg.liftReferenceDeg <= 180)) return false;
    }
    if (model.gaitStance) {
        for (size_t gait = 0; gait < gaitCount; ++gait) {
            for (uint8_t mask : model.gaitStance[gait]) {
                if (mask != inferStance && mask > 15) return false;
            }
        }
    }
    return true;
}

enum class SupportSource : uint8_t { FootHeight, FactoryPhase, CustomPose };
enum class Tracking : uint8_t { Uncalibrated, Reference, Estimated, Gap, NoSupport, Inconsistent };

struct PlanarStep {
    Tracking tracking;
    float xMm, yMm, yawRad, residualMm;
};

// Map the current body-relative feet to the previous body frame. For planted feet,
// this transform is also the body's relative movement.
inline PlanarStep fitStance(const LegPoints (&previous)[legCount],
                           const LegPoints (&current)[legCount], uint8_t support, float residualLimit) {
    Vec3 p{}, q{};
    unsigned count = 0;
    for (size_t i = 0; i < legCount; ++i) {
        if (!(support & (1u << i))) continue;
        p = p + previous[i].foot;
        q = q + current[i].foot;
        ++count;
    }
    if (count < 2) return {Tracking::NoSupport, 0, 0, 0, 0};
    p = p * (1.f / count);
    q = q * (1.f / count);

    float cross = 0, aligned = 0;
    for (size_t i = 0; i < legCount; ++i) {
        if (!(support & (1u << i))) continue;
        const auto a = current[i].foot - q;
        const auto b = previous[i].foot - p;
        cross += a.x * b.y - a.y * b.x;
        aligned += a.x * b.x + a.y * b.y;
    }
    // Reject coincident points, including nearly coincident support on a 1 mm baseline.
    if (hypotf(cross, aligned) < 1.f) return {Tracking::NoSupport, 0, 0, 0, 0};
    const float yaw = atan2f(cross, aligned);
    const Vec3 zAxis{0, 0, 1};
    const auto shift = p - rotate(q, zAxis, yaw);
    float residual = 0;
    for (size_t i = 0; i < legCount; ++i) {
        if (!(support & (1u << i))) continue;
        const auto error = rotate(current[i].foot, zAxis, yaw) + shift - previous[i].foot;
        const float distance = sqrtf(dot(error, error));
        if (distance > residual) residual = distance;
    }
    if (residual > residualLimit) return {Tracking::Inconsistent, 0, 0, 0, residual};
    return {Tracking::Estimated, shift.x, shift.y, yaw, residual};
}

struct Odometry {
    Tracking tracking = Tracking::Uncalibrated;
    bool continuous = false;
    uint32_t epoch = 0;
    uint32_t rejected = 0;
    SupportSource supportSource = SupportSource::FootHeight;
    uint8_t support = 0;
    uint8_t sharedSupport = 0;
    float xMm = 0, yMm = 0, yawRad = 0, travelMm = 0, residualMm = 0;
    float stepXMm = 0, stepYMm = 0, stepYawRad = 0;
    LegPoints legs[legCount] = {};
};

class LegTracker {
public:
    explicit LegTracker(const LegCalibration& model) : model_(model), calibrated_(validCalibration(model)) {}

    const Odometry& state() const { return state_; }
    bool calibrated() const { return calibrated_; }

    void zero() {
        const uint32_t epoch = state_.epoch + 1;
        state_ = {};
        state_.epoch = epoch;
        previous_ = {};
        receivedUs_ = 0;
    }

    void sample(const Message& sample, int64_t receivedUs) {
        if (!calibrated_) return;
        LegPoints points[legCount];
        for (size_t i = 0; i < legCount; ++i) {
            points[i] = legPoints(model_.legs[i], sample.angles[hipChannel[i]], sample.angles[liftChannel[i]]);
        }
        uint8_t support = sample.stance;
        state_.supportSource = SupportSource::CustomPose;
        if (support == inferStance && model_.gaitStance && isWalk(sample.action) &&
            (sample.value & 255) == uint16_t(State::Walking) && (sample.value >> 8) < gaitFrames) {
            const size_t gait = uint8_t(sample.action) - uint8_t(Action::Forward);
            support = model_.gaitStance[gait][sample.value >> 8];
            state_.supportSource = SupportSource::FactoryPhase;
        }
        if (support == inferStance) {
            support = inferredSupport(points);
            state_.supportSource = SupportSource::FootHeight;
        }
        state_.sharedSupport = 0;
        state_.residualMm = 0;
        state_.stepXMm = state_.stepYMm = state_.stepYawRad = 0;
        if (!receivedUs_) {
            state_.tracking = Tracking::Reference;
            state_.continuous = true;
        } else if (previous_.boot != sample.boot || sample.sequence - previous_.sequence == 0 ||
                   sample.sequence - previous_.sequence > 3 * jointPeriodMs ||
                   receivedUs - receivedUs_ > 100000) {
            reject(Tracking::Gap);
        } else {
            state_.sharedSupport = state_.support & support;
            const auto step = fitStance(state_.legs, points, state_.sharedSupport, model_.residualLimitMm);
            state_.residualMm = step.residualMm;
            if (step.tracking == Tracking::Estimated) {
                const auto shift = rotate({step.xMm, step.yMm, 0}, {0, 0, 1}, state_.yawRad);
                state_.xMm += shift.x;
                state_.yMm += shift.y;
                state_.yawRad = remainderf(state_.yawRad + step.yawRad, 2 * pi);
                state_.travelMm += hypotf(step.xMm, step.yMm);
                state_.stepXMm = step.xMm;
                state_.stepYMm = step.yMm;
                state_.stepYawRad = step.yawRad;
                state_.tracking = step.tracking;
            } else {
                reject(step.tracking);
            }
        }
        memcpy(state_.legs, points, sizeof(points));
        state_.support = support;
        previous_ = sample;
        receivedUs_ = receivedUs;
    }

private:
    uint8_t inferredSupport(const LegPoints (&points)[legCount]) const {
        float floor = points[0].foot.z;
        for (const auto& point : points) {
            if (point.foot.z < floor) floor = point.foot.z;
        }
        uint8_t mask = 0;
        for (size_t i = 0; i < legCount; ++i) {
            if (points[i].foot.z - floor <= model_.supportBandMm) mask |= 1u << i;
        }
        return mask;
    }

    void reject(Tracking reason) {
        state_.tracking = reason;
        state_.continuous = false;
        ++state_.rejected;
    }

    const LegCalibration& model_;
    const bool calibrated_;
    Odometry state_{};
    Message previous_{};
    int64_t receivedUs_ = 0;
};

}  // namespace spider
