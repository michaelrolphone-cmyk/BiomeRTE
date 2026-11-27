#include <Arduino.h>
#include <ArduinoJson.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <mbedtls/md.h>

#include "camera_config.h"
#include "control.h"
#if __has_include("leg_calibration.h")
#include "leg_calibration.h"
#else
#include "leg_calibration.example.h"
#endif
#include "sensors.h"

namespace {

using namespace spider;

constexpr int64_t sensorPeriodUs = 50000;
constexpr int64_t imagePeriodUs = 200000;
constexpr int64_t socketIdleUs = 3000000;
constexpr int64_t uartRefreshUs = 70000;
constexpr int64_t uartStatusTimeoutUs = 200000;
constexpr size_t imageBufferBytes = 512 * 1024;
constexpr char controlKey[] = SPIDER_ROVER_CONTROL_SECRET;
constexpr bool controlEnabled = sizeof(controlKey) >= 33;
constexpr char sensorToken[] = SPIDER_ROVER_SENSOR_TOKEN;
constexpr char roverHost[] = SPIDER_ROVER_HOST;
constexpr char wifiSsid[] = SPIDER_WIFI_SSID;
static_assert(sizeof(receiverId) > 1 && sizeof(receiverId) <= 65,
              "Use a nonempty receiver ID of up to 64 bytes");

HardwareSerial motionSerial(1);
Decoder decoder;
LegTracker legTracker(legCalibration);
char bootId[17];
char authHeader[256];

struct SharedControl {
    Intent intent{};
    uint32_t update = 0;
    uint32_t generation = 0;
    uint32_t lastForwarded = 0;
    uint32_t lastAcknowledged = 0;
    int64_t statusUs = 0;
    State state = State::Idle;
    Action action = Action::None;
    bool socketConnected = false;
    uint32_t originRequest = 0;
    uint32_t lastOrigin = 0;
    int64_t jointsUs = 0;
    Message joints{};
    Odometry odometry{};
};

SharedControl motion;
portMUX_TYPE motionLock = portMUX_INITIALIZER_UNLOCKED;

const char* actionName(Action action) {
    for (const auto& entry : actions) {
        if (entry.action == action) return entry.name;
    }
    return "none";
}

Action parseAction(const char* name) {
    for (const auto& entry : actions) {
        if (!strcmp(entry.name, name)) return entry.action;
    }
    return Action::None;
}

bool constantTimeHexMatch(const char* signature, const uint8_t* digest, size_t size) {
    if (strlen(signature) != 2 * size) return false;
    constexpr char hex[] = "0123456789abcdef";
    unsigned diff = 0;
    for (size_t i = 0; i < size; ++i) {
        diff |= signature[2 * i] ^ hex[digest[i] >> 4];
        diff |= signature[2 * i + 1] ^ hex[digest[i] & 15];
    }
    return diff == 0;
}

const char* trackingName(Tracking tracking) {
    switch (tracking) {
        case Tracking::Reference: return "origin";
        case Tracking::Estimated: return "estimated";
        case Tracking::Gap: return "sample_gap";
        case Tracking::NoSupport: return "insufficient_support";
        case Tracking::Inconsistent: return "inconsistent_support";
        default: return "uncalibrated";
    }
}

void appendPoint(JsonObject leg, const char* key, Vec3 position) {
    auto row = leg[key].to<JsonArray>();
    row.add(position.x);
    row.add(position.y);
    row.add(position.z);
}

void appendJointTracking(JsonObject status, const SharedControl& state, int64_t tick) {
    const bool available = state.jointsUs > 0 && state.jointsUs <= tick;
    const bool fresh = available && tick - state.jointsUs <= 100000;
    status["joint_pose_available"] = fresh;
    status["last_origin_sequence"] = state.lastOrigin;
    auto joints = status["joints"].to<JsonObject>();
    joints["fresh"] = fresh;
    joints["position_source"] = "trimmed_pwm_target";
    if (available) {
        joints["actuator_boot"] = state.joints.boot;
        joints["actuator_uptime_ms"] = state.joints.sequence;
        joints["received_monotonic_us"] = state.jointsUs;
        joints["keyframe"] = state.joints.value >> 8;
        joints["state"] = state.joints.value & 255;
        joints["action"] = actionName(state.joints.action);
        auto targets = joints["targets_deg"].to<JsonArray>();
        for (auto angle : state.joints.angles) targets.add(angle);
    }
    const auto& result = state.odometry;
    auto odom = status["servo_odometry"].to<JsonObject>();
    odom["source"] = "servo_targets_assumed_stance";
    const bool calibrationValid = legTracker.calibrated();
    odom["calibration_valid"] = calibrationValid;
    if (!calibrationValid) odom["status"] = "uncalibrated";
    else if (!available || result.tracking == Tracking::Uncalibrated) odom["status"] = "waiting_for_sample";
    else odom["status"] = fresh ? trackingName(result.tracking) : "stale";
    odom["origin_epoch"] = result.epoch;
    odom["continuous_from_origin"] = fresh && result.continuous;
    if (!available || result.tracking == Tracking::Uncalibrated) return;
    odom["calibration_revision"] = legCalibration.revision;
    const char* supportSource = "foot_height";
    if (result.supportSource == SupportSource::FactoryPhase) supportSource = "factory_phase";
    if (result.supportSource == SupportSource::CustomPose) supportSource = "authored";
    odom["support_source"] = supportSource;
    odom["assumed_support_mask"] = result.support;
    odom["shared_support_mask"] = result.sharedSupport;
    odom["rejected_steps"] = result.rejected;
    if (result.tracking == Tracking::Estimated || result.tracking == Tracking::Inconsistent) {
        odom["fit_residual_mm"] = result.residualMm;
    }
    if (fresh && result.tracking == Tracking::Estimated) {
        auto delta = odom["latest_body_delta"].to<JsonObject>();
        delta["x_mm"] = result.stepXMm;
        delta["y_mm"] = result.stepYMm;
        delta["yaw_rad"] = result.stepYawRad;
    }
    odom["x_mm"] = result.xMm;
    odom["y_mm"] = result.yMm;
    odom["heading_rad"] = result.yawRad;
    odom["displacement_mm"] = hypotf(result.xMm, result.yMm);
    odom["integrated_travel_mm"] = result.travelMm;
    auto legs = status["legs_body_mm"].to<JsonObject>();
    constexpr const char* names[] = {"front_right", "rear_right", "front_left", "rear_left"};
    for (size_t i = 0; i < legCount; ++i) {
        auto leg = legs[names[i]].to<JsonObject>();
        appendPoint(leg, "hip", result.legs[i].hip);
        appendPoint(leg, "lift_joint", result.legs[i].lift);
        appendPoint(leg, "foot", result.legs[i].foot);
    }
}

bool readCustomPose(const JsonDocument& request, Message& pose) {
    const auto angles = request["angles_deg"].as<JsonArrayConst>();
    if (angles.size() != jointCount ||
        !request["duration_ms"].is<uint16_t>() || !request["stance_mask"].is<uint8_t>()) return false;
    pose = {Kind::JointPose, Action::Custom, 0, request["duration_ms"].as<uint16_t>()};
    pose.stance = request["stance_mask"].as<uint8_t>();
    for (size_t i = 0; i < jointCount; ++i) {
        if (!angles[i].is<uint8_t>()) return false;
        pose.angles[i] = angles[i].as<uint8_t>();
    }
    return validJointPose(pose);
}

class RoverSocket {
public:
    explicit RoverSocket(bool camera) : camera_(camera) {}

    void begin() {
        socket_.begin(roverHost, SPIDER_ROVER_PORT, camera_ ? "/camera" : "/sensors", "");
        socket_.setAuthorization(authHeader);
        socket_.setReconnectInterval(1000);
        socket_.onEvent([this](WStype_t type, uint8_t* data, size_t size) { onEvent(type, data, size); });
    }

    void linkLost() {
        if (connected_) socket_.disconnect();
    }

    void poll() {
        socket_.loop();
        if (connected_ && esp_timer_get_time() - lastClockUs_ > socketIdleUs) {
            socket_.disconnect();
        }
    }

    void publishSensors() {
        const int64_t tick = esp_timer_get_time();
        if (!connected_ || !clockReady_ || tick - lastPublishedUs_ < sensorPeriodUs) return;
        lastPublishedUs_ = tick;
        JsonDocument packet;
        makePacket(packet, false, tick);
        appendSensors(packet, tick);
        appendStatus(packet, tick);
        packet["delivery_mode"] = "continuous";
        packet["stream_sequence"] = ++sensorSequence_;
        packet["image_available"] = false;
        packet["capture_timestamp_semantics"] = "sensor_snapshot_tick_no_camera_frame";
        packet["served_monotonic_us"] = esp_timer_get_time();
        String payload;
        if (serializeJson(packet, payload)) socket_.sendTXT(payload);
    }

    void publishCamera(uint8_t* buffer) {
        const int64_t now = esp_timer_get_time();
        if (!connected_ || !cameraReady_ || inFlight_[0] || now - lastPublishedUs_ < imagePeriodUs) return;
        lastPublishedUs_ = now;
        camera_fb_t* image = esp_camera_fb_get();
        if (!image) return;
        const int64_t captureUs = int64_t(image->timestamp.tv_sec) * 1000000 + image->timestamp.tv_usec;
        const int64_t servedUs = esp_timer_get_time();
        if (captureUs <= lastImageUs_ || captureUs > servedUs || servedUs - captureUs > 500000 ||
            image->format != PIXFORMAT_JPEG || image->len >= imageBufferBytes) {
            esp_camera_fb_return(image);
            return;
        }
        lastImageUs_ = captureUs;
        JsonDocument packet;
        makePacket(packet, true, captureUs);
        packet["width"] = image->width;
        packet["height"] = image->height;
        packet["image_available"] = true;
        packet["capture_timestamp_semantics"] = "first_dma_buffer_monotonic_us";
        const size_t metadataSize = measureJson(packet);
        const size_t frameSize = 4 + metadataSize + image->len;
        if (frameSize > imageBufferBytes) {
            esp_camera_fb_return(image);
            return;
        }
        for (unsigned i = 0; i < 4; ++i) buffer[3 - i] = metadataSize >> (8 * i);
        serializeJson(packet, reinterpret_cast<char*>(buffer + 4), metadataSize + 1);
        memcpy(buffer + 4 + metadataSize, image->buf, image->len);
        esp_camera_fb_return(image);
        strlcpy(inFlight_, packet["frame_id"] | "", sizeof(inFlight_));
        sentImageUs_ = esp_timer_get_time();
        if (!socket_.sendBIN(buffer, frameSize)) socket_.disconnect();
    }

    void checkImageAck() {
        if (inFlight_[0] && esp_timer_get_time() - sentImageUs_ > socketIdleUs) socket_.disconnect();
    }

private:
    void makePacket(JsonDocument& packet, bool image, int64_t tick) {
        char frameId[48];
        snprintf(frameId, sizeof(frameId), "%s:%lld", bootId, (long long)tick);
        packet["schema"] = image ? "biome.capture.v1" : "biome.sensor_tick.v1";
        packet["receiver_id"] = receiverId;
        packet["camera_id"] = cameraRole;
        packet["boot_id"] = bootId;
        packet["frame_id"] = String(frameId);
        packet["capture_clock"] = "esp_timer";
        packet["capture_monotonic_us"] = tick;
        packet["served_monotonic_us"] = esp_timer_get_time();
    }

    void appendStatus(JsonDocument& packet, int64_t tick) {
        portENTER_CRITICAL(&motionLock);
        const SharedControl state = motion;
        portEXIT_CRITICAL(&motionLock);
        auto status = packet["spider_motion"].to<JsonObject>();
        status["enabled"] = controlEnabled;
        status["session"] = session_;
        status["uart_ready"] = state.statusUs > 0 && state.statusUs <= tick &&
            tick - state.statusUs < uartStatusTimeoutUs;
        status["last_accepted_sequence"] = acceptedSequence_;
        status["last_forwarded_sequence"] = state.lastForwarded;
        status["last_acknowledged_sequence"] = state.lastAcknowledged;
        status["state"] = unsigned(state.state);
        status["action"] = actionName(state.action);
        appendJointTracking(status, state, tick);
    }

    void routeCommand(JsonDocument& request, int64_t now) {
        if (!controlEnabled || !clockReady_ ||
            strcmp(request["session"] | "", session_) ||
            strcmp(request["receiver_id"] | "", receiverId) ||
            !request["sequence"].is<uint32_t>() ||
            !request["issued_monotonic_us"].is<int64_t>() ||
            !request["expires_monotonic_us"].is<int64_t>()) return;
        const char* mode = request["mode"] | "";
        const char* name = request["action"] | "";
        Kind kind;
        if (!strcmp(mode, "walk")) kind = Kind::Walk;
        else if (!strcmp(mode, "pose")) kind = Kind::Pose;
        else if (!strcmp(mode, "stop") && !strcmp(name, "none")) kind = Kind::Stop;
        else if (!strcmp(mode, "joint_pose") && !strcmp(name, "custom")) kind = Kind::JointPose;
        else if (!strcmp(mode, "origin") && !strcmp(name, "zero")) kind = Kind::Origin;
        else return;
        const Action action = parseAction(name);
        if (kind != Kind::Stop && action == Action::None) return;
        Intent intent{kind, action, request["sequence"].as<uint32_t>(),
                            request["issued_monotonic_us"].as<int64_t>(),
                            request["expires_monotonic_us"].as<int64_t>()};
        if (kind == Kind::JointPose && !readCustomPose(request, intent.pose)) return;
        char message[256];
        int size = snprintf(message, sizeof(message), "spider_command.v1\n%s\n%s\n%lu\n%s\n%s\n%lld\n%lld",
            session_, receiverId, (unsigned long)intent.sequence, mode, name,
            (long long)intent.issuedUs, (long long)intent.expiresUs);
        if (size <= 0 || size >= int(sizeof(message))) return;
        if (kind == Kind::JointPose) {
            const auto& a = intent.pose.angles;
            const int written = snprintf(message + size, sizeof(message) - size,
                "\n%u\n%u\n%u,%u,%u,%u,%u,%u,%u,%u", unsigned(intent.pose.value), unsigned(intent.pose.stance),
                unsigned(a[0]), unsigned(a[1]), unsigned(a[2]), unsigned(a[3]), unsigned(a[4]),
                unsigned(a[5]), unsigned(a[6]), unsigned(a[7]));
            if (written <= 0 || written >= int(sizeof(message)) - size) return;
            size += written;
        }
        uint8_t digest[32];
        if (mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256),
                           reinterpret_cast<const uint8_t*>(controlKey), sizeof(controlKey) - 1,
                           reinterpret_cast<const uint8_t*>(message), size, digest) != 0 ||
            !constantTimeHexMatch(request["signature"] | "", digest, sizeof(digest)) ||
            !gate_.accept(intent, now)) return;
        portENTER_CRITICAL(&motionLock);
        if (kind == Kind::Origin) {
            motion.originRequest = intent.sequence;
        } else {
            motion.intent = intent;
            ++motion.update;
        }
        portEXIT_CRITICAL(&motionLock);
        acceptedSequence_ = intent.sequence;
    }

    void onEvent(WStype_t type, uint8_t* data, size_t size) {
        if (type == WStype_CONNECTED || type == WStype_DISCONNECTED) {
            connected_ = type == WStype_CONNECTED;
            cameraReady_ = clockReady_ = false;
            inFlight_[0] = 0;
            lastClockUs_ = esp_timer_get_time();
            if (!camera_) {
                snprintf(session_, sizeof(session_), "%08lx%08lx", (unsigned long)esp_random(),
                         (unsigned long)esp_random());
                gate_.reset();
                acceptedSequence_ = 0;
                portENTER_CRITICAL(&motionLock);
                ++motion.generation;
                ++motion.update;
                motion.intent = {};
                motion.lastForwarded = motion.lastAcknowledged = 0;
                motion.originRequest = motion.lastOrigin = 0;
                motion.socketConnected = connected_;
                portEXIT_CRITICAL(&motionLock);
            }
            return;
        }
        if (type != WStype_TEXT || !connected_ || size > 1024) return;
        const int64_t received = esp_timer_get_time();
        JsonDocument request;
        if (deserializeJson(request, data, size, DeserializationOption::NestingLimit(3))) return;
        const char* name = request["type"] | "";
        if (!strcmp(name, "clock")) {
            const char* id = request["id"] | "";
            if (!id[0] || strlen(id) > 64) return;
            JsonDocument reply;
            reply["type"] = "clock";
            reply["id"] = id;
            reply["boot_id"] = bootId;
            reply["received_monotonic_us"] = received;
            reply["served_monotonic_us"] = esp_timer_get_time();
            String payload;
            if (serializeJson(reply, payload) && socket_.sendTXT(payload)) {
                lastClockUs_ = received;
                clockReady_ = true;
            }
        } else if (camera_ && !strcmp(name, "camera_ready") && !strcmp(request["boot_id"] | "", bootId)) {
            cameraReady_ = clockReady_;
        } else if (camera_ && !strcmp(name, "camera_ack") && !strcmp(request["frame_id"] | "", inFlight_)) {
            inFlight_[0] = 0;
        } else if (!camera_ && !strcmp(name, "spider_command.v1")) {
            routeCommand(request, received);
        }
    }

    WebSocketsClient socket_;
    const bool camera_;
    bool connected_ = false;
    bool clockReady_ = false;
    bool cameraReady_ = false;
    int64_t lastClockUs_ = 0;
    int64_t lastPublishedUs_ = 0;
    int64_t lastImageUs_ = 0;
    int64_t sentImageUs_ = 0;
    uint64_t sensorSequence_ = 0;
    char inFlight_[48] = {};
    char session_[17] = {};
    CommandGate gate_;
    uint32_t acceptedSequence_ = 0;
};

bool configureCamera() {
    camera_config_t config{};
    config.pin_pwdn = config.pin_reset = -1;
    config.pin_d0 = pins::cameraData[0];
    config.pin_d1 = pins::cameraData[1];
    config.pin_d2 = pins::cameraData[2];
    config.pin_d3 = pins::cameraData[3];
    config.pin_d4 = pins::cameraData[4];
    config.pin_d5 = pins::cameraData[5];
    config.pin_d6 = pins::cameraData[6];
    config.pin_d7 = pins::cameraData[7];
    config.pin_xclk = pins::xclk;
    config.pin_pclk = pins::pclk;
    config.pin_vsync = pins::vsync;
    config.pin_href = pins::href;
    config.pin_sccb_sda = pins::siod;
    config.pin_sccb_scl = pins::sioc;
    config.xclk_freq_hz = 20000000;
    config.ledc_timer = LEDC_TIMER_0;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.pixel_format = PIXFORMAT_JPEG;
    config.frame_size = FRAMESIZE_HD;
    config.jpeg_quality = 14;
    config.fb_count = 2;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    config.grab_mode = CAMERA_GRAB_LATEST;
    return esp_camera_init(&config) == ESP_OK;
}

void sensorSocketWorker(void*) {
    RoverSocket link(false);
    link.begin();
    for (;;) {
        if (WiFi.status() == WL_CONNECTED) {
            link.poll();
            link.publishSensors();
        } else link.linkLost();
        delay(2);
    }
}

void cameraSocketWorker(void*) {
    auto* buffer = static_cast<uint8_t*>(heap_caps_malloc(imageBufferBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buffer && configureCamera()) {
        RoverSocket link(true);
        link.begin();
        for (;;) {
            if (WiFi.status() == WL_CONNECTED) {
                link.poll();
                link.checkImageAck();
                link.publishCamera(buffer);
            } else link.linkLost();
            delay(2);
        }
    }
    free(buffer);
    Serial.println("Spider camera initialization failed");
    vTaskDelete(nullptr);
}

struct SentCommand {
    uint32_t uartSequence;
    uint32_t socketSequence;
    uint32_t generation;
};

uint32_t uartSequence = 0;
int64_t lastUartSendUs = 0;
SentCommand sent[8] = {};

bool sendUart(Message message, uint32_t sequence, uint32_t generation) {
    const uint32_t next = uartSequence + 1;
    message.sequence = next;
    uint8_t bytes[maxWireSize];
    const size_t length = encode(message, bytes);
    if (motionSerial.availableForWrite() < int(length)) return false;
    if (motionSerial.write(bytes, length) != length) return false;
    uartSequence = next;
    sent[next % 8] = {next, sequence, generation};
    lastUartSendUs = esp_timer_get_time();
    if (sequence) {
        portENTER_CRITICAL(&motionLock);
        if (motion.generation == generation) motion.lastForwarded = sequence;
        portEXIT_CRITICAL(&motionLock);
    }
    return true;
}

void observeJoints(const Message& sample) {
    if (sample.action > Action::Custom || (sample.value & 255) > uint16_t(State::Returning) ||
        (sample.stance != inferStance && sample.stance > 15)) return;
    for (auto angle : sample.angles) {
        if (angle < 1 || angle > 180) return;
    }
    const int64_t now = esp_timer_get_time();
    legTracker.sample(sample, now);
    portENTER_CRITICAL(&motionLock);
    motion.jointsUs = now;
    motion.joints = sample;
    motion.odometry = legTracker.state();
    portEXIT_CRITICAL(&motionLock);
}

void readUart() {
    for (int count = 0; count < 96 && motionSerial.available(); ++count) {
        Message reply{};
        if (!decoder.feed(motionSerial.read(), reply)) continue;
        if (reply.kind == Kind::JointSample) {
            observeJoints(reply);
            continue;
        }
        if (reply.kind != Kind::Status || reply.value > uint16_t(State::Returning) ||
            reply.action > Action::Custom) continue;
        const auto& command = sent[reply.sequence % 8];
        if (!reply.sequence || command.uartSequence != reply.sequence) continue;
        portENTER_CRITICAL(&motionLock);
        motion.statusUs = esp_timer_get_time();
        motion.state = State(reply.value);
        motion.action = reply.action;
        if (motion.generation == command.generation && command.socketSequence) {
            motion.lastAcknowledged = max(motion.lastAcknowledged, command.socketSequence);
        }
        portEXIT_CRITICAL(&motionLock);
        sent[reply.sequence % 8] = {};
    }
}

void driveUart() {
    static uint32_t lastUpdate = 0;
    static uint32_t lastGeneration = 0;
    static Intent desired{};
    static bool poseActive = false;
    static bool stopPending = false;
    portENTER_CRITICAL(&motionLock);
    const uint32_t reset = motion.originRequest;
    if (reset) {
        motion.originRequest = 0;
        legTracker.zero();
        motion.odometry = legTracker.state();
        motion.lastOrigin = reset;
    }
    portEXIT_CRITICAL(&motionLock);
    readUart();
    const int64_t now = esp_timer_get_time();
    portENTER_CRITICAL(&motionLock);
    const SharedControl state = motion;
    portEXIT_CRITICAL(&motionLock);

    if (lastGeneration != state.generation) {
        stopPending |= desired.kind == Kind::Walk || poseActive;
        lastGeneration = state.generation;
        desired = {};
        poseActive = false;
    }
    if (lastUpdate != state.update) {
        lastUpdate = state.update;
        desired = state.intent;
        poseActive = false;
    }
    const bool uartReady = state.statusUs && now - state.statusUs < uartStatusTimeoutUs;
    const bool moving = state.state == State::Walking || state.state == State::Posing;
    if (moving && (!state.socketConnected || !uartReady || desired.kind == Kind{})) {
        stopPending = true;
    }
    if (desired.kind == Kind::Walk && (!uartReady || desired.expiresUs - now < 1000)) {
        stopPending = true;
        desired = {};
    }
    if (stopPending || desired.kind == Kind::Stop) {
        const uint32_t sequence = desired.kind == Kind::Stop ? desired.sequence : 0;
        if (sendUart({Kind::Stop, Action::None}, sequence, state.generation)) {
            stopPending = false;
            if (sequence) desired = {};
        }
        return;
    }
    if (poseActive && state.lastAcknowledged == desired.sequence &&
        state.state == State::Idle && state.action == desired.action) {
        desired = {};
        poseActive = false;
    }
    const bool jointPose = desired.kind == Kind::JointPose;
    const bool jointsFresh = state.jointsUs && now - state.jointsUs <= 100000;
    if ((desired.kind == Kind::Pose || jointPose) && !poseActive) {
        if (!uartReady || (jointPose && !jointsFresh) || desired.expiresUs <= now) {
            desired = {};
        } else {
            const Message command = jointPose ? desired.pose : Message{Kind::Pose, desired.action};
            poseActive = sendUart(command, desired.sequence, state.generation);
        }
    }
    if (desired.kind == Kind::Walk) {
        if (now - lastUartSendUs >= uartRefreshUs || state.lastForwarded != desired.sequence) {
            const uint16_t remaining = (desired.expiresUs - now) / 1000;
            sendUart({Kind::Walk, desired.action, 0, remaining}, desired.sequence, state.generation);
        }
    } else if (now - lastUartSendUs >= uartRefreshUs) {
        sendUart({Kind::Probe, Action::None}, 0, state.generation);
    }
}

}  // namespace

void setup() {
    Serial.begin(115200);
    motionSerial.begin(spider::uartBaud, SERIAL_8N1, spider::pins::uartRx, spider::pins::uartTx);
    snprintf(bootId, sizeof(bootId), "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
    if (strlen(sensorToken) < 32 || strlen(sensorToken) > 200 ||
        strpbrk(sensorToken, "\r\n") || !*roverHost || !*wifiSsid) {
        Serial.println("Configure Wi-Fi, Rover host and sensor token in include/site_config.h");
        return;
    }
    snprintf(authHeader, sizeof(authHeader), "Sensor %s", sensorToken);
    if (!startSensors()) Serial.println("A spider sensor worker did not start");
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);
    WiFi.begin(wifiSsid, SPIDER_WIFI_PASSWORD);
    if (xTaskCreate(sensorSocketWorker, "spider-socket", 12288, nullptr, 3, nullptr) != pdPASS)
        Serial.println("Spider sensor socket worker did not start");
    if (xTaskCreate(cameraSocketWorker, "spider-camera", 12288, nullptr, 1, nullptr) != pdPASS)
        Serial.println("Spider camera socket worker did not start");
}

void loop() {
    driveUart();
    delay(2);
}
