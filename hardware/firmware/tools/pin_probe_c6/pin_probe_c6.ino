// ESP32-C6: report pins that something external pulls up (sensor module pull-up resistor)
const int pins[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 18, 19, 20, 21, 22, 23};

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== C6 pin probe ===");
}

void loop() {
  Serial.print("pulled-up pins:");
  int n = 0;
  for (int p : pins) {
    pinMode(p, INPUT_PULLDOWN);
    delay(3);
    if (digitalRead(p)) { Serial.printf(" G%d", p); n++; }
  }
  Serial.println(n ? "" : " none");
  delay(1000);
}
