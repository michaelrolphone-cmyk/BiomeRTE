#pragma once

#include "spider_uart.h"

namespace spider {

constexpr int64_t maximumCommandUs = int64_t(maximumWalkMs) * 1000;

struct Intent {
    Kind kind;
    Action action;
    uint32_t sequence;
    int64_t issuedUs;
    int64_t expiresUs;
};

class CommandGate {
public:
    void reset() { last_ = 0; }

    bool accept(const Intent& intent, int64_t nowUs) {
        if (intent.sequence <= last_ || intent.issuedUs < 0 || intent.issuedUs > nowUs ||
            intent.expiresUs <= nowUs || intent.expiresUs - intent.issuedUs > maximumCommandUs) {
            return false;
        }
        if ((intent.kind != Kind::Walk && intent.kind != Kind::Pose && intent.kind != Kind::Stop) ||
            !isCommand({intent.kind, intent.action, 0, uint16_t(intent.kind == Kind::Walk ? 1 : 0)})) {
            return false;
        }
        last_ = intent.sequence;
        return true;
    }

private:
    uint32_t last_ = 0;
};

}  // namespace spider
