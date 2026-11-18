// ANCHOR ENGINE node (ESP32-WROOM-32E) -> AE "engine" (originator CEngine) on cse-ship
//  vibration : ADXL345 RMS over 1 s (200 Hz sampling), 1 Hz  (SDA 21, SCL 22, addr 0x53)
//  gas       : MQ-2 sensor voltage, 0.5 Hz (AO -> 10k/20k divider -> GPIO34)
#include <Wire.h>
#include <anchor_engine_tls.h>
#include <AnchorNode.h>

const uint8_t ADXL = 0x53;
const int PIN_MQ2 = 34;
const uint32_t MQ2_WARMUP_MS = 120000;  // readings before this are flagged warm=false

AnchorNode node("CEngine", "engine", "Nanchor.engine");
bool adxlOk = false;
#ifndef ANCHOR_ENABLE_DDS_SOURCE
#define ANCHOR_ENABLE_DDS_SOURCE 1
#endif
AnchorRawVibration rawVibration;

void adxlWrite(uint8_t r, uint8_t v) {
  Wire.beginTransmission(ADXL);
  Wire.write(r);
  Wire.write(v);
  Wire.endTransmission();
}
bool adxlRead(float &x, float &y, float &z) {
  Wire.beginTransmission(ADXL);
  Wire.write(0x32);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(ADXL, (uint8_t)6) != 6) return false;
  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = Wire.read();
  x = (int16_t)(b[1] << 8 | b[0]) * 0.0039f;
  y = (int16_t)(b[3] << 8 | b[2]) * 0.0039f;
  z = (int16_t)(b[5] << 8 | b[4]) * 0.0039f;
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ANCHOR ENGINE node ===");
  Wire.begin(21, 22, 400000);
  Wire.beginTransmission(ADXL);
  Wire.write(0x00);
  Wire.endTransmission(false);
  Wire.requestFrom(ADXL, (uint8_t)1);
  adxlOk = Wire.available() && Wire.read() == 0xE5;
  if (adxlOk) {
    adxlWrite(0x2C, 0x0B);  // 200 Hz
    adxlWrite(0x31, 0x09);  // full res, +-4 g
    adxlWrite(0x2D, 0x08);  // measure
  }
  Serial.printf("[ADXL345] %s\n", adxlOk ? "OK" : "NOT FOUND");
  analogSetPinAttenuation(PIN_MQ2, ADC_11db);
  node.addContainer("vibration");
  node.addContainer("gas");
  node.enableReportProfile();
  node.begin();
}

// running stats for one 1-s window
uint32_t tSample = 0, tGas = 0, tStat = 0;
int n = 0;
double sm = 0, sm2 = 0, sz = 0;
float peak = 0;
uint32_t activeWindowMs = 1000, windowStartedAt = 0;

void loop() {
  node.loop();
  uint32_t now = millis();
  uint32_t requestedWindow = node.reportIntervalMs();
  if (requestedWindow != activeWindowMs) {
    activeWindowMs = requestedWindow;
    n = 0; sm = sm2 = sz = 0; peak = 0;
    windowStartedAt = now;
  }

  if (adxlOk && now - tSample >= 5) {
    tSample = now;
    float x, y, z;
    if (adxlRead(x, y, z)) {
#if ANCHOR_ENABLE_DDS_SOURCE
      rawVibration.add(AnchorNode::nowMs(), micros(), x, y, z);
#endif
      if (!n) windowStartedAt = now;
      float m = sqrtf(x * x + y * y + z * z);
      sm += m;
      sm2 += (double)m * m;
      sz += z;
      if (n > 0) peak = max(peak, fabsf(m - (float)(sm / (n + 1))));
      n++;
    }
    if (n >= (int)(activeWindowMs / 5)) {
      double mean = sm / n;
      float rms = sqrt(max(0.0, sm2 / n - mean * mean));
      char buf[200];
      snprintf(buf, sizeof buf, "{\"vib_rms_g\":%.4f,\"vib_peak_g\":%.3f,\"mean_g\":%.3f,\"n\":%d,\"rssi\":%d,\"reportIntervalMs\":%lu,\"sampleWindowMs\":%lu}", rms, peak,
               mean, n, node.rssi(), (unsigned long)activeWindowMs, (unsigned long)(now - windowStartedAt));
      if (node.publish("vibration", buf)) Serial.printf("[TX] vibration %s\n", buf);
      n = 0;
      sm = sm2 = sz = 0;
      peak = 0;
    }
  }

#if ANCHOR_ENABLE_DDS_SOURCE
  if (rawVibration.due(micros())) {
    node.publishRawVibration(rawVibration);
    rawVibration.clear();  // Live data is never replayed over the durable CIN queue.
  }
#endif

  if (now - tGas >= 2000) {
    tGas = now;
    uint32_t mv = 0;
    for (int i = 0; i < 16; i++) mv += analogReadMilliVolts(PIN_MQ2);
    float sensorV = mv / 16.0f * 1.5f / 1000.0f;  // undo the 10k/20k divider
    char buf[120];
    snprintf(buf, sizeof buf, "{\"mq2_v\":%.3f,\"warm\":%s,\"uptime_s\":%lu}", sensorV,
             now > MQ2_WARMUP_MS ? "true" : "false", (unsigned long)(now / 1000));
    if (node.publish("gas", buf)) Serial.printf("[TX] gas %s\n", buf);
  }

  if (now - tStat >= 10000) {
    tStat = now;
    node.printStats();
  }
}
