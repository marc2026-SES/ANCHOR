// ESP32-S3: SDA fixed on GPIO8, try every usable pin as SCL at low speed with internal pull-ups
#include <Wire.h>

const int PIN_SDA = 8;
const int sclCandidates[] = {1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17, 21, 38, 39, 40, 41, 42, 45, 46, 47, 48};

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== S3 SCL finder (SDA=G8) ===");
  int hits = 0;
  for (int scl : sclCandidates) {
    if (!Wire.begin(PIN_SDA, scl, 50000)) continue;
    pinMode(PIN_SDA, INPUT_PULLUP);  // Wire keeps the pins in open-drain; add weak pull-ups
    pinMode(scl, INPUT_PULLUP);
    Wire.begin(PIN_SDA, scl, 50000);
    for (uint8_t a = 1; a < 127; a++) {
      Wire.beginTransmission(a);
      if (Wire.endTransmission() == 0) {
        Serial.printf("FOUND SCL=G%d addr=0x%02X\n", scl, a);
        hits++;
      }
    }
    Wire.end();
  }
  Serial.println(hits ? "=== done ===" : "no device on any SCL pin\n=== done ===");
}

void loop() {}
