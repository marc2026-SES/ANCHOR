// BRIDGE node sensor test (ESP32-S3)
// MPU6050 on I2C (SDA 8, SCL 9, addr 0x68), GPS on UART1 (GPS TX -> GPIO18, GPS RX <- GPIO17)
#include <Wire.h>

const int PIN_SDA = 8;
const int PIN_SCL = 9;
const int PIN_GPS_RX = 18;
const int PIN_GPS_TX = 17;
uint8_t mpuAddr = 0;

bool mpuWrite(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(mpuAddr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

bool mpuRead(uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(mpuAddr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(mpuAddr, len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

void pinLevels() {
  // With the internal pull-down on, a pin reads 1 only if something drives or pulls it up
  const int pins[] = {PIN_SDA, PIN_SCL, PIN_GPS_RX};
  Serial.print("[PINS] pull-down test (1 = pulled up / driven):");
  for (int p : pins) {
    pinMode(p, INPUT_PULLDOWN);
    delay(5);
    Serial.printf(" G%d=%d", p, digitalRead(p));
  }
  Serial.println();
}

void i2cInit() {
  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  Serial.print("[I2C] scan:");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      if (a == 0x68 || a == 0x69) mpuAddr = a;
      found++;
    }
  }
  Serial.println(found ? "" : " none");
  if (!mpuAddr) {
    Serial.println("[MPU6050] NOT FOUND");
    return;
  }
  uint8_t who = 0;
  mpuRead(0x75, &who, 1);
  Serial.printf("[MPU6050] addr=0x%02X WHO_AM_I=0x%02X (0x68 genuine, 0x70/0x72/0x98 clones also OK)\n", mpuAddr, who);
  mpuWrite(0x6B, 0x00);  // wake up
  mpuWrite(0x1C, 0x00);  // accel +-2 g
  mpuWrite(0x1B, 0x00);  // gyro +-250 dps
}

// GPS: find the baud rate that yields NMEA sentences
uint32_t gpsBaud = 0;
void gpsInit() {
  const uint32_t bauds[] = {9600, 38400, 115200, 4800};
  for (uint32_t b : bauds) {
    Serial1.begin(b, SERIAL_8N1, PIN_GPS_RX, PIN_GPS_TX);
    uint32_t t0 = millis();
    int dollars = 0, bytes = 0;
    while (millis() - t0 < 2500) {
      while (Serial1.available()) {
        char c = Serial1.read();
        bytes++;
        if (c == '$') dollars++;
      }
    }
    Serial.printf("[GPS] baud %6lu: %d bytes, %d sentences\n", (unsigned long)b, bytes, dollars);
    if (dollars >= 2) {
      gpsBaud = b;
      return;
    }
    Serial1.end();
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ANCHOR BRIDGE sensor test ===");
  pinLevels();
  i2cInit();
  gpsInit();
  Serial.printf("[GPS] %s\n", gpsBaud ? "NMEA OK" : "NO DATA");
}

String line;
uint32_t lastMpu = 0;

void loop() {
  if (gpsBaud) {
    while (Serial1.available()) {
      char c = Serial1.read();
      if (c == '\n') {
        line.trim();
        // RMC: status A = fix, V = no fix. GGA: field 7 = satellites in use
        if (line.startsWith("$GPRMC") || line.startsWith("$GNRMC") ||
            line.startsWith("$GPGGA") || line.startsWith("$GNGGA")) {
          Serial.printf("[GPS] %s\n", line.c_str());
        }
        line = "";
      } else if (line.length() < 120) {
        line += c;
      }
    }
  }

  if (mpuAddr && millis() - lastMpu >= 1000) {
    lastMpu = millis();
    uint8_t b[14];
    if (mpuRead(0x3B, b, 14)) {
      float ax = (int16_t)(b[0] << 8 | b[1]) / 16384.0f;
      float ay = (int16_t)(b[2] << 8 | b[3]) / 16384.0f;
      float az = (int16_t)(b[4] << 8 | b[5]) / 16384.0f;
      float tc = (int16_t)(b[6] << 8 | b[7]) / 340.0f + 36.53f;
      float gx = (int16_t)(b[8] << 8 | b[9]) / 131.0f;
      float gy = (int16_t)(b[10] << 8 | b[11]) / 131.0f;
      float gz = (int16_t)(b[12] << 8 | b[13]) / 131.0f;
      float roll = atan2f(ay, az) * 57.2958f;
      float pitch = atan2f(-ax, sqrtf(ay * ay + az * az)) * 57.2958f;
      Serial.printf("[MPU6050] a=(%+.2f,%+.2f,%+.2f) g  gyro=(%+.1f,%+.1f,%+.1f) dps  roll=%+.1f pitch=%+.1f  temp=%.1fC\n",
                    ax, ay, az, gx, gy, gz, roll, pitch, tc);
    } else {
      Serial.println("[MPU6050] read failed");
    }
  }
}
