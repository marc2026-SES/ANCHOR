#pragma once
#include <stdint.h>

// Separate, lossy live stream. Never consumes the durable oneM2M CIN sequence.
struct AnchorRawVibration {
  static const unsigned CAPACITY = 20;
  unsigned count = 0;
  uint64_t startedAtMs = 0;
  uint32_t startedUs = 0;
  uint32_t offsetsUs[CAPACITY];
  float xyzG[CAPACITY][3];

  bool add(uint64_t epochMs, uint32_t us, float x, float y, float z) {
    if (!epochMs || count >= CAPACITY) return false;
    if (!count) { startedAtMs = epochMs; startedUs = us; }
    offsetsUs[count] = us - startedUs;  // unsigned subtraction handles micros() wrap
    xyzG[count][0] = x; xyzG[count][1] = y; xyzG[count][2] = z;
    ++count;
    return true;
  }
  bool due(uint32_t us) const {
    return count && (count == CAPACITY || us - startedUs >= 100000);
  }
  void clear() { count = 0; }
};
