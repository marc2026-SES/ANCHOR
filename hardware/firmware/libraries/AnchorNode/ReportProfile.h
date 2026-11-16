#pragma once
#include <stdint.h>

// Pure timing policy, host-tested as well as used by the ESP32 helper.
struct AnchorReportProfile {
  uint32_t intervalMs = 1000;
  uint64_t untilMs = 0;
  static bool valid(uint32_t interval, uint32_t duration) {
    return interval >= 200 && interval <= 5000 && interval % 5 == 0 &&
           duration >= 1000 && duration <= 300000;
  }
  uint32_t current(uint64_t now) const {
    // Before clock sync and after expiration, use the safe default cadence.
    return now && now < untilMs ? intervalMs : 1000;
  }
};
