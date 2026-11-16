// ANCHOR BRIDGE node (ESP32-S3) -> AE "bridge" (originator CBridge) on cse-ship
//  attitude : MPU6050 roll/pitch/gyro, 1 Hz   (SDA 8, SCL 9)
//  position : GPS fix/lat/lon/sats/speed, 0.5 Hz (GPS TX -> GPIO18, RX <- GPIO17)
// Keep the board still for ~2 s after reset: the gyro bias is calibrated at boot.
// Mounting: components up, X arrow toward the bow -> roll + = starboard down, pitch + = bow down.
// Zero: write con "zero" to cse-ship/bridge/command (dashboard button); the current roll/pitch
// becomes 0 and is stored in flash. "unzero" clears it.
#include <Wire.h>
#include <Preferences.h>
#include <AnchorNode.h>

const int PIN_SDA = 8, PIN_SCL = 9, PIN_GPS_RX = 18, PIN_GPS_TX = 17;
const uint8_t MPU = 0x68;

AnchorNode node("CBridge", "bridge", "Nanchor.bridge");
bool mpuOk = false;
float gb[3] = {0, 0, 0};  // gyro bias, dps
float roll0 = 0, pitch0 = 0;  // level offsets from the "zero" command, deg
Preferences prefs;
bool zeroRequested = false;

// ---------- MPU6050 ----------
void mpuWrite(uint8_t r, uint8_t v) {
  Wire.beginTransmission(MPU);
  Wire.write(r);
  Wire.write(v);
  Wire.endTransmission();
}
bool mpuRead(float a[3], float g[3]) {
  Wire.beginTransmission(MPU);
  Wire.write(0x3B);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(MPU, (uint8_t)14) != 14) return false;
  uint8_t b[14];
  for (int i = 0; i < 14; i++) b[i] = Wire.read();
  for (int i = 0; i < 3; i++) {
    a[i] = (int16_t)(b[2 * i] << 8 | b[2 * i + 1]) / 16384.0f;
    g[i] = (int16_t)(b[8 + 2 * i] << 8 | b[9 + 2 * i]) / 131.0f;
  }
  return true;
}
void mpuInit() {
  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  Wire.beginTransmission(MPU);
  mpuOk = Wire.endTransmission() == 0;
  if (!mpuOk) {
    Serial.println("[MPU6050] NOT FOUND");
    return;
  }
  mpuWrite(0x6B, 0x00);  // wake
  mpuWrite(0x1A, 0x03);  // DLPF ~44 Hz
  mpuWrite(0x1C, 0x00);  // +-2 g
  mpuWrite(0x1B, 0x00);  // +-250 dps
  delay(100);
  float a[3], g[3], s[3] = {0, 0, 0};
  int n = 0;
  for (int i = 0; i < 200; i++) {
    if (mpuRead(a, g)) {
      for (int k = 0; k < 3; k++) s[k] += g[k];
      n++;
    }
    delay(5);
  }
  for (int k = 0; k < 3; k++) gb[k] = n ? s[k] / n : 0;
  Serial.printf("[MPU6050] OK, gyro bias=(%+.2f,%+.2f,%+.2f) dps\n", gb[0], gb[1], gb[2]);
}

void rawAttitude(const float a[3], float &roll, float &pitch) {
  roll = atan2f(a[1], a[2]) * 57.2958f;
  pitch = atan2f(-a[0], sqrtf(a[1] * a[1] + a[2] * a[2])) * 57.2958f;
}

// Average 2 s of accelerometer-derived angles and store them as the new level.
void doZero() {
  float a[3], g[3], sr = 0, sp = 0;
  int n = 0;
  for (int i = 0; i < 100; i++) {
    if (mpuRead(a, g)) {
      float r, p;
      rawAttitude(a, r, p);
      sr += r;
      sp += p;
      n++;
    }
    delay(20);
  }
  if (!n) return;
  roll0 = sr / n;
  pitch0 = sp / n;
  prefs.putFloat("roll0", roll0);
  prefs.putFloat("pitch0", pitch0);
  Serial.printf("[ZERO] level set: roll0=%.2f pitch0=%.2f (saved)\n", roll0, pitch0);
}

// ---------- GPS (NMEA RMC + GGA) ----------
struct {
  bool fix = false;
  double lat = 0, lon = 0;
  int sats = 0;
  float sog = 0, cog = 0, hdop = 0;
  uint32_t lastSentence = 0;
} gps;
String nmea;

double nmeaDeg(const String &v, const String &hemi) {
  if (v.length() < 4) return 0;
  double x = v.toDouble();
  int deg = (int)(x / 100);
  double d = deg + (x - deg * 100) / 60.0;
  return (hemi == "S" || hemi == "W") ? -d : d;
}
String field(const String &s, int idx) {
  int start = 0;
  for (int i = 0; i < idx; i++) {
    start = s.indexOf(',', start) + 1;
    if (start == 0) return "";
  }
  int end = s.indexOf(',', start);
  if (end < 0) end = s.indexOf('*', start);
  return s.substring(start, end < 0 ? s.length() : end);
}
void gpsLine(const String &s) {
  if (s.length() < 6 || s[0] != '$') return;
  String t = s.substring(3, 6);
  if (t == "RMC") {
    gps.lastSentence = millis();
    gps.fix = field(s, 2) == "A";
    if (gps.fix) {
      gps.lat = nmeaDeg(field(s, 3), field(s, 4));
      gps.lon = nmeaDeg(field(s, 5), field(s, 6));
      gps.sog = field(s, 7).toFloat();
      gps.cog = field(s, 8).toFloat();
    }
  } else if (t == "GGA") {
    gps.sats = field(s, 7).toInt();
    gps.hdop = field(s, 8).toFloat();
  }
}
void gpsPoll() {
  while (Serial1.available()) {
    char c = Serial1.read();
    if (c == '\n') {
      nmea.trim();
      gpsLine(nmea);
      nmea = "";
    } else if (nmea.length() < 120) {
      nmea += c;
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ANCHOR BRIDGE node ===");
  mpuInit();
  prefs.begin("bridge", false);
  roll0 = prefs.getFloat("roll0", 0);
  pitch0 = prefs.getFloat("pitch0", 0);
  Serial.printf("[ZERO] stored offsets roll0=%.2f pitch0=%.2f\n", roll0, pitch0);
  Serial1.begin(9600, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
  node.addContainer("attitude");
  node.addContainer("position");
  node.enableCommands([](const String &cmd) {
    if (cmd == "zero") zeroRequested = true;  // run outside the MQTT callback
    if (cmd == "unzero") {
      roll0 = pitch0 = 0;
      prefs.putFloat("roll0", 0);
      prefs.putFloat("pitch0", 0);
      Serial.println("[ZERO] cleared");
    }
  });
  node.enableReportProfile();
  node.begin();
}

uint32_t tAtt = 0, tPos = 0, tStat = 0, tLive = 0, tFiltUs = 0;
float fRoll = 0, fPitch = 0, lastA[3] = {0, 0, 1}, lastG[3] = {0, 0, 0};
bool filtInit = false;
uint32_t lastImuReadMs = 0;

void loop() {
  node.loop();
  gpsPoll();
  uint32_t now = millis();

  if (zeroRequested && mpuOk) {
    zeroRequested = false;
    doZero();
    tAtt = 0;  // publish the zeroed attitude right away
  }

  // 100 Hz complementary filter: the gyro tracks fast motion, the accelerometer pins the long-term
  // level (accel-only angles jump whenever the hull accelerates). roll rate = gx, pitch rate = gy.
  if (mpuOk && micros() - tFiltUs >= 10000) {
    uint32_t nowUs = micros();
    float dt = tFiltUs ? (nowUs - tFiltUs) / 1e6f : 0;
    tFiltUs = nowUs;
    if (mpuRead(lastA, lastG)) {
      lastImuReadMs = millis();
      for (int k = 0; k < 3; k++) lastG[k] -= gb[k];
      float ar, ap;
      rawAttitude(lastA, ar, ap);
      if (!filtInit || dt > 0.5f) {
        fRoll = ar;
        fPitch = ap;
        filtInit = true;
      } else {
        const float K = 0.98f;  // ~0.5 s time constant at 100 Hz
        fRoll = K * (fRoll + lastG[0] * dt) + (1 - K) * ar;
        fPitch = K * (fPitch + lastG[1] * dt) + (1 - K) * ap;
      }
    }
  }

  // 20 Hz live stream for the dashboard's 3D view (plain MQTT, not stored in the CSE)
  if (filtInit && now - tLive >= 50) {
    tLive = now;
    char buf[64];
    snprintf(buf, sizeof buf, "{\"r\":%.2f,\"p\":%.2f}", fRoll - roll0, fPitch - pitch0);
    node.publishLive("anchor/live/bridge/attitude", buf);
  }

  // 1 Hz oneM2M record
  if (filtInit && now - tAtt >= node.reportIntervalMs()) {
    tAtt = now;
    char buf[320];
    snprintf(buf, sizeof buf,
             "{\"roll\":%.1f,\"pitch\":%.1f,\"gx\":%.1f,\"gy\":%.1f,\"gz\":%.1f,\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,\"roll0\":%.2f,\"pitch0\":%.2f,\"rssi\":%d,\"reportIntervalMs\":%lu,\"ok\":%s}",
             fRoll - roll0, fPitch - pitch0, lastG[0], lastG[1], lastG[2], lastA[0], lastA[1], lastA[2], roll0, pitch0,
             node.rssi(), (unsigned long)node.reportIntervalMs(), millis() - lastImuReadMs <= 100 ? "true" : "false");
    if (node.publish("attitude", buf)) Serial.printf("[TX] attitude %s\n", buf);
  }

  if (now - tPos >= 2000) {
    tPos = now;
    bool alive = gps.lastSentence && now - gps.lastSentence < 3000;
    char buf[200];
    snprintf(buf, sizeof buf,
             "{\"gps\":\"%s\",\"fix\":%s,\"lat\":%.6f,\"lon\":%.6f,\"sats\":%d,\"hdop\":%.1f,\"sog_kn\":%.2f,\"cog\":%.1f}",
             alive ? "ok" : "no_data", gps.fix ? "true" : "false", gps.lat, gps.lon, gps.sats, gps.hdop, gps.sog, gps.cog);
    if (node.publish("position", buf)) Serial.printf("[TX] position %s\n", buf);
  }

  if (now - tStat >= 10000) {
    tStat = now;
    node.printStats();
  }
}
