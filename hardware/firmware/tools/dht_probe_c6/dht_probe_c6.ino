// ESP32-C6: raw DHT11 diagnosis without a library
// Sends the start pulse on GPIO4 and records every level change on the line for 10 ms.
//  - 0 edges         : sensor not answering (data line not connected / sensor dead)
//  - ~84 edges       : sensor answers; full 40-bit frame is decoded below
const int PIN = 4;

int captureEdges(uint32_t *times, int *levels, int maxN) {
  pinMode(PIN, OUTPUT);
  digitalWrite(PIN, LOW);
  delay(20);  // start signal: >= 18 ms low
  pinMode(PIN, INPUT_PULLUP);
  noInterrupts();
  uint32_t t0 = micros();
  int last = digitalRead(PIN);
  int n = 0;
  while (micros() - t0 < 10000 && n < maxN) {
    int v = digitalRead(PIN);
    if (v != last) {
      times[n] = micros() - t0;
      levels[n] = v;
      n++;
      last = v;
    }
  }
  interrupts();
  return n;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== C6 DHT11 raw probe (GPIO4) ===");
  pinMode(PIN, INPUT);
  delay(5);
  Serial.printf("idle level, no pull: %d\n", digitalRead(PIN));
  pinMode(PIN, INPUT_PULLDOWN);
  delay(5);
  Serial.printf("idle level, pull-down: %d (1 = module has its own pull-up)\n", digitalRead(PIN));
  pinMode(PIN, INPUT_PULLUP);
  delay(5);
  Serial.printf("idle level, pull-up: %d (0 = line held low / shorted to GND)\n", digitalRead(PIN));
  delay(1500);
}

void loop() {
  static uint32_t times[120];
  static int levels[120];
  int n = captureEdges(times, levels, 120);
  Serial.printf("edges=%d", n);
  if (n > 0) {
    Serial.print("  first:");
    for (int i = 0; i < n && i < 6; i++) Serial.printf(" %s@%luus", levels[i] ? "H" : "L", (unsigned long)times[i]);
  }
  Serial.println();

  // Decode: after the response (L,H) each bit is L(~50us) then H (26-28us = 0, ~70us = 1)
  if (n >= 82) {
    uint8_t data[5] = {0};
    int bit = 0;
    for (int i = 0; i + 1 < n && bit < 40; i++) {
      if (levels[i] == 1 && i >= 3) {  // rising edge = start of a data high pulse
        uint32_t high = times[i + 1] - times[i];
        data[bit / 8] <<= 1;
        if (high > 45) data[bit / 8] |= 1;
        bit++;
      }
    }
    uint8_t sum = data[0] + data[1] + data[2] + data[3];
    Serial.printf("  decoded: RH=%u.%u%%  T=%u.%uC  checksum %s\n", data[0], data[1], data[2], data[3],
                  sum == data[4] ? "OK" : "BAD");
  }
  delay(2500);
}
