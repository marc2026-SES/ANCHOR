// CARGO node sensor test (ESP32-C6)
// DHT11 DATA -> GPIO4, MC-38 door switch between GPIO5 and GND (internal pull-up)
#include <DHT.h>

const int PIN_DHT = 4;
const int PIN_HATCH = 5;

DHT dht(PIN_DHT, DHT11);
int lastHatch = -1;
uint32_t lastDht = 0;
int ok = 0, fail = 0;

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ANCHOR CARGO sensor test ===");
  pinMode(PIN_DHT, INPUT_PULLDOWN);
  delay(5);
  Serial.printf("[PINS] DHT data line idle=%d (1 = pull-up present)\n", digitalRead(PIN_DHT));
  dht.begin();
  pinMode(PIN_HATCH, INPUT_PULLUP);
}

void loop() {
  // MC-38 (NO): magnet near -> contact closed -> LOW = hatch closed
  int h = digitalRead(PIN_HATCH);
  if (h != lastHatch) {
    Serial.printf("[HATCH] %s (GPIO5=%d)\n", h == LOW ? "CLOSED" : "OPEN", h);
    lastHatch = h;
  }

  // DHT11 needs >= 2 s between reads
  if (millis() - lastDht >= 2500) {
    lastDht = millis();
    float t = dht.readTemperature();
    float rh = dht.readHumidity();
    if (isnan(t) || isnan(rh)) {
      fail++;
      Serial.printf("[DHT11] read failed (ok=%d fail=%d)\n", ok, fail);
    } else {
      ok++;
      Serial.printf("[DHT11] temp=%.1f C  humidity=%.0f %%  (ok=%d fail=%d)\n", t, rh, ok, fail);
    }
  }
  delay(20);
}
