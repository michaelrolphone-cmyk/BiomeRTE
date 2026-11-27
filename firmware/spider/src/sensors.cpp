#include "sensors.h"

#include <Adafruit_BNO08x.h>
#include <Arduino.h>
#include <SPI.h>
#include <SparkFun_VL53L5CX_IO.h>
#include <vl53l5cx_api.h>
#include <Wire.h>
#include <esp_timer.h>

#include "camera_config.h"

namespace spider {
namespace {

constexpr uint8_t tofMuxAddress = 0x70;
constexpr uint8_t tofAddress = 0x29;
constexpr int64_t lidarFreshUs = 500000;
constexpr int64_t imuFreshUs = 250000;
constexpr int64_t retryUs = 5000000;

struct Grid {
    int64_t observedUs;
    int16_t distance[64];
    uint8_t status[64];
    bool present;
};

struct ImuReport {
    int64_t observedUs;
    uint8_t accuracy;
    float values[4];
};

struct Reading {
    Grid grids[2];
    ImuReport rotation, acceleration, gyro, magnetic, linearAcceleration;
    bool imuPresent;
    bool muxPresent;
};

Reading latest{};
portMUX_TYPE readingLock = portMUX_INITIALIZER_UNLOCKED;
SparkFun_VL53L5CX_IO tofBus[2];
VL53L5CX_Configuration lidar[2];
VL53L5CX_ResultsData lidarResult;
SPIClass imuSpi(FSPI);
Adafruit_BNO08x imu(pins::imuReset);

bool selectTof(uint8_t channel, bool mux) {
    if (!mux) return channel == 0;
    Wire.beginTransmission(tofMuxAddress);
    Wire.write(1 << channel);
    return Wire.endTransmission() == 0;
}

bool startRanging(uint8_t index) {
    auto& device = lidar[index];
    // Allocate each driver once. SparkFun's begin() allocates on every retry.
    memset(&device, 0, sizeof(device));
    if (!tofBus[index].begin(tofAddress, Wire)) return false;
    device.platform.address = tofAddress;
    device.platform.VL53L5CX_i2c = &tofBus[index];
    uint8_t alive = 0;
    return vl53l5cx_is_alive(&device, &alive) == 0 && alive &&
        vl53l5cx_init(&device) == 0 && vl53l5cx_set_resolution(&device, 64) == 0 &&
        vl53l5cx_set_ranging_frequency_hz(&device, 10) == 0 &&
        vl53l5cx_start_ranging(&device) == 0;
}

void lidarWorker(void*) {
    Wire.begin(pins::tofSda, pins::tofScl, 400000);
    Wire.setTimeOut(20);
    Wire.beginTransmission(tofMuxAddress);
    const bool mux = Wire.endTransmission() == 0;
    portENTER_CRITICAL(&readingLock);
    latest.muxPresent = mux;
    portEXIT_CRITICAL(&readingLock);
    int64_t nextAttempt[2] = {};
    bool online[2] = {};
    for (;;) {
        for (uint8_t i = 0; i < (mux ? 2 : 1); ++i) {
            const int64_t now = esp_timer_get_time();
            if (!online[i] && now < nextAttempt[i]) continue;
            const bool selected = selectTof(i, mux);
            if (!online[i]) {
                online[i] = selected && startRanging(i);
                nextAttempt[i] = esp_timer_get_time() + retryUs;
            }
            Grid fresh{};
            bool gotSample = false;
            const int64_t observed = esp_timer_get_time();
            uint8_t ready = 0;
            if (online[i] && selected && vl53l5cx_check_data_ready(&lidar[i], &ready) == 0 &&
                ready && vl53l5cx_get_ranging_data(&lidar[i], &lidarResult) == 0) {
                fresh.observedUs = observed;
                for (size_t zone = 0; zone < 64; ++zone) {
                    fresh.distance[zone] = lidarResult.distance_mm[zone];
                    fresh.status[zone] = lidarResult.target_status[zone];
                }
                gotSample = true;
            }
            portENTER_CRITICAL(&readingLock);
            auto& previous = latest.grids[i];
            if (gotSample) previous = fresh;
            if (previous.observedUs && observed - previous.observedUs > 2000000) {
                previous = {};
                online[i] = false;
                nextAttempt[i] = observed + retryUs;
            } else if (online[i] && !previous.observedUs && observed >= nextAttempt[i]) {
                online[i] = false;
            }
            previous.present = online[i];
            portEXIT_CRITICAL(&readingLock);
        }
        delay(10);
    }
}

bool enableImuReports() {
    bool all = true;
    for (sh2_SensorId_t report : {SH2_GAME_ROTATION_VECTOR, SH2_ACCELEROMETER,
                                 SH2_GYROSCOPE_CALIBRATED, SH2_MAGNETIC_FIELD_CALIBRATED,
                                 SH2_LINEAR_ACCELERATION}) {
        all &= imu.enableReport(report, report == SH2_MAGNETIC_FIELD_CALIBRATED ? 50000 : 10000);
    }
    return all;
}

void recordImu(const sh2_SensorValue_t& sample) {
    ImuReport reading{esp_timer_get_time(), sample.status, {}};
    ImuReport* slot = nullptr;
    switch (sample.sensorId) {
        case SH2_GAME_ROTATION_VECTOR: {
            const auto& q = sample.un.gameRotationVector;
            slot = &latest.rotation;
            reading.values[0] = q.real;
            reading.values[1] = q.i;
            reading.values[2] = q.j;
            reading.values[3] = q.k;
            break;
        }
        case SH2_ACCELEROMETER:
            slot = &latest.acceleration;
            reading.values[0] = sample.un.accelerometer.x;
            reading.values[1] = sample.un.accelerometer.y;
            reading.values[2] = sample.un.accelerometer.z;
            break;
        case SH2_GYROSCOPE_CALIBRATED:
            slot = &latest.gyro;
            reading.values[0] = sample.un.gyroscope.x;
            reading.values[1] = sample.un.gyroscope.y;
            reading.values[2] = sample.un.gyroscope.z;
            break;
        case SH2_MAGNETIC_FIELD_CALIBRATED:
            slot = &latest.magnetic;
            reading.values[0] = sample.un.magneticField.x;
            reading.values[1] = sample.un.magneticField.y;
            reading.values[2] = sample.un.magneticField.z;
            break;
        case SH2_LINEAR_ACCELERATION:
            slot = &latest.linearAcceleration;
            reading.values[0] = sample.un.linearAcceleration.x;
            reading.values[1] = sample.un.linearAcceleration.y;
            reading.values[2] = sample.un.linearAcceleration.z;
            break;
        default:
            return;
    }
    portENTER_CRITICAL(&readingLock);
    *slot = reading;
    portEXIT_CRITICAL(&readingLock);
}

void clearImu(bool present) {
    portENTER_CRITICAL(&readingLock);
    latest.imuPresent = present;
    latest.rotation = latest.acceleration = latest.gyro = latest.magnetic = latest.linearAcceleration = {};
    portEXIT_CRITICAL(&readingLock);
}

void imuWorker(void*) {
    imuSpi.begin(pins::imuSck, pins::imuMiso, pins::imuMosi, pins::imuCs);
    for (;;) {
        const bool present = imu.begin_SPI(pins::imuCs, pins::imuInt, &imuSpi);
        clearImu(present);
        if (present && enableImuReports()) {
            int64_t lastReport = esp_timer_get_time();
            while (esp_timer_get_time() - lastReport < 2000000) {
                if (imu.wasReset()) {
                    clearImu(true);
                    if (!enableImuReports()) break;
                }
                sh2_SensorValue_t event;
                for (int i = 0; i < 12 && imu.getSensorEvent(&event); ++i) {
                    recordImu(event);
                    lastReport = esp_timer_get_time();
                }
                delay(2);
            }
        }
        clearImu(false);
        delay(2000);
    }
}

bool fresh(int64_t observedUs, int64_t tickUs, int64_t maximumAgeUs) {
    return observedUs > 0 && observedUs <= tickUs && tickUs - observedUs <= maximumAgeUs;
}

void appendReport(JsonObject parent, const char* name, const ImuReport& report, int64_t tickUs,
                  size_t size) {
    auto row = parent[name].to<JsonObject>();
    const bool valid = fresh(report.observedUs, tickUs, imuFreshUs);
    row["valid"] = valid;
    if (!valid) return;
    row["observed_monotonic_us"] = report.observedUs;
    row["accuracy"] = report.accuracy;
    auto values = row["values"].to<JsonArray>();
    for (size_t i = 0; i < size; ++i) values.add(report.values[i]);
}

}  // namespace

bool startSensors() {
    const bool tofStarted = xTaskCreate(lidarWorker, "spider-tof", 4096, nullptr, 1, nullptr) == pdPASS;
    const bool imuStarted = xTaskCreate(imuWorker, "spider-imu", 4096, nullptr, 2, nullptr) == pdPASS;
    return tofStarted && imuStarted;
}

void appendSensors(JsonDocument& packet, int64_t tickUs) {
    Reading snapshot;
    portENTER_CRITICAL(&readingLock);
    snapshot = latest;
    portEXIT_CRITICAL(&readingLock);
    auto tof = packet["lidar"].to<JsonObject>();
    tof["model"] = "VL53L5CX";
    auto grids = tof["sensors"].to<JsonArray>();
    for (size_t i = 0; i < (snapshot.muxPresent ? 2u : 1u); ++i) {
        const auto& grid = snapshot.grids[i];
        auto row = grids.add<JsonObject>();
        row["id"] = i == 0 ? "spider-tof-0" : "spider-tof-1";
        row["resolution"] = 64;
        row["present"] = grid.present;
        const bool valid = grid.present && fresh(grid.observedUs, tickUs, lidarFreshUs);
        row["valid"] = valid;
        if (!valid) continue;
        row["observed_monotonic_us"] = grid.observedUs;
        auto distances = row["distance_mm"].to<JsonArray>();
        auto statuses = row["target_status"].to<JsonArray>();
        for (size_t zone = 0; zone < 64; ++zone) {
            distances.add(grid.distance[zone]);
            statuses.add(grid.status[zone]);
        }
    }
    auto bno = packet["bno085"].to<JsonObject>();
    bno["present"] = snapshot.imuPresent;
    bno["frame"] = "bno085_native_xyz";
    bno["heading_reference"] = "relative_yaw";
    appendReport(bno, "game_rotation_wxyz", snapshot.rotation, tickUs, 4);
    appendReport(bno, "acceleration_m_s2", snapshot.acceleration, tickUs, 3);
    appendReport(bno, "gyroscope_rad_s", snapshot.gyro, tickUs, 3);
    appendReport(bno, "magnetic_field_uT", snapshot.magnetic, tickUs, 3);
    appendReport(bno, "linear_acceleration_m_s2", snapshot.linearAcceleration, tickUs, 3);
}

}  // namespace spider
