#pragma once

#include "leg_odometry.h"

namespace spider {

// The kit's outer dimensions do not locate its servo axes. Measure all four
// assembled legs before supplying a revision, thresholds, and these vectors.
constexpr LegCalibration legCalibration{
    "", // Calibration revision. Empty leaves metric telemetry unavailable.
    {
        {}, // Front right
        {}, // Rear right
        {}, // Front left
        {}, // Rear left
    },
    0, // Height band for inferred floor contact, in mm.
    0, // Maximum allowed planted-foot fit residual, in mm.
    nullptr, // Optional factory gait support masks after floor validation.
};

}  // namespace spider
