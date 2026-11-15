// ANCHOR CARGO node (ESP32-C6) -> AE "cargo" (originator CCargo) on cse-ship
//  climate : DHT11 temperature/humidity, every 5 s (DATA -> GPIO4)
//  hatch   : MC-38 door switch GPIO5<->GND (pull-up), on every change + every 30 s
#include <DHT.h>
#include <AnchorNode.h>

const int PIN_DHT = 4, PIN_HATCH = 5;

AnchorNode node("CCargo", "cargo", "Nanchor.cargo");
DHT dht(PIN_DHT, DHT11);

int hatch = -1, hatchRaw = -1;
uint32_t hatchChangedAt = 0, openSince = 0, changes = 0;

void sendHatch(const char *reason) {
  char buf[140];
  snprintf(buf, sizeof buf, "{\"state\":\"%s\",\"event\":\"%s\",\"open_s\":%lu,\"changes\":%lu}",
           hatch == LOW ? "closed" : "open", reason,
           (unsigned long)(hatch == LOW ? 0 : (millis() - openSince) / 1000), (unsigned long)changes);
  if (node.publish("hatch", buf)) Serial.printf("[TX] hatch %s\n", buf);
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ANCHOR CARGO node ===");
  dht.begin();
  pinMode(PIN_HATCH, INPUT_PULLUP);
  hatch = hatchRaw = digitalRead(PIN_HATCH);
  openSince = millis();
  node.addContainer("climate");
  node.addContainer("hatch");
  node.begin();
}

uint32_t tClimate = 0, tHatch = 0, tStat = 0;
bool sentInitial = false;

void loop() {
  node.loop();
  uint32_t now = millis();

  // debounced hatch (MC-38 NO: magnet near -> closed -> LOW)
  int r = digitalRead(PIN_HATCH);
  if (r != hatchRaw) {
    hatchRaw = r;
    hatchChangedAt = now;
  }
  if (r != hatch && now - hatchChangedAt > 50) {
    hatch = r;
    changes++;
    if (hatch == HIGH) openSince = now;
    Serial.printf("[HATCH] %s\n", hatch == LOW ? "CLOSED" : "OPEN");
    sendHatch(hatch == LOW ? "closed" : "opened");
    tHatch = now;
  }
  if (node.ready() && (!sentInitial || now - tHatch >= 30000)) {
    sentInitial = true;
    tHatch = now;
    sendHatch("periodic");
  }

  if (now - tClimate >= 5000) {
    tClimate = now;
    float t = dht.readTemperature(), h = dht.readHumidity();
    char buf[120];
    if (isnan(t) || isnan(h))
      snprintf(buf, sizeof buf, "{\"ok\":false,\"rssi\":%d}", node.rssi());
    else
      snprintf(buf, sizeof buf, "{\"ok\":true,\"temp_c\":%.1f,\"rh\":%.0f,\"rssi\":%d}", t, h, node.rssi());
    if (node.publish("climate", buf)) Serial.printf("[TX] climate %s\n", buf);
  }

  if (now - tStat >= 10000) {
    tStat = now;
    node.printStats();
  }
}
