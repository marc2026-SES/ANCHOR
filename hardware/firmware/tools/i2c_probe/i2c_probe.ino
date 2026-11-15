// Scan I2C on several SDA/SCL pin pairs to find where a sensor is actually wired
#include <Wire.h>

const int pairs[][2] = {{21, 22}, {22, 21}, {18, 19}, {19, 18}, {23, 22}, {21, 23}, {25, 26}, {26, 25}, {32, 33}, {33, 32}};

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== I2C pin probe ===");
  for (auto &p : pairs) {
    Wire.begin(p[0], p[1], 100000);
    Serial.printf("SDA=%2d SCL=%2d :", p[0], p[1]);
    int found = 0;
    for (uint8_t a = 1; a < 127; a++) {
      Wire.beginTransmission(a);
      if (Wire.endTransmission() == 0) { Serial.printf(" 0x%02X", a); found++; }
    }
    Serial.println(found ? "" : " -");
    Wire.end();
  }
  // With the internal pull-down on, a pin still reads 1 only if an external pull-up
  // (the sensor module's resistor, powered from its VCC) is connected to it.
  const int pins[] = {21, 22, 18, 19, 23, 25, 26, 32, 33};
  Serial.print("pull-down test (1 = external pull-up present):");
  for (int pin : pins) {
    pinMode(pin, INPUT_PULLDOWN);
    delay(5);
    Serial.printf(" G%d=%d", pin, digitalRead(pin));
  }
  Serial.println("\n=== done ===");
}

void loop() {}
