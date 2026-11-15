// ENGINE node sensor test (ESP32-WROOM-32E)
// ADXL345 on I2C (SDA 21, SCL 22, addr 0x53), MQ-2 AO via 10k/20k divider on GPIO34
#include <Wire.h>

const int PIN_SDA = 21;
const int PIN_SCL = 22;
const int PIN_MQ2 = 34;
const uint8_t ADXL_ADDR = 0x53;
const float DIVIDER_RATIO = 1.5f;  // (10k + 20k) / 20k

bool adxlOk = false;

void writeReg(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ADXL_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

bool readRegs(uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(ADXL_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(ADXL_ADDR, len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

void i2cScan() {
  Serial.print("[I2C] scan:");
  int found = 0;
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", a);
      found++;
    }
  }
  Serial.println(found ? "" : " none");
}

bool adxlInit() {
  uint8_t id = 0;
  if (!readRegs(0x00, &id, 1)) return false;
  Serial.printf("[ADXL345] DEVID=0x%02X (expect 0xE5)\n", id);
  if (id != 0xE5) return false;
  writeReg(0x2C, 0x0B);  // BW_RATE 200 Hz
  writeReg(0x31, 0x09);  // DATA_FORMAT full-res, +-4 g
  writeReg(0x2D, 0x08);  // POWER_CTL measure
  return true;
}

bool adxlRead(float &x, float &y, float &z) {
  uint8_t b[6];
  if (!readRegs(0x32, b, 6)) return false;
  x = (int16_t)(b[1] << 8 | b[0]) * 0.0039f;
  y = (int16_t)(b[3] << 8 | b[2]) * 0.0039f;
  z = (int16_t)(b[5] << 8 | b[4]) * 0.0039f;
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ANCHOR ENGINE sensor test ===");
  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  i2cScan();
  adxlOk = adxlInit();
  Serial.printf("[ADXL345] %s\n", adxlOk ? "OK" : "NOT FOUND");
  analogSetPinAttenuation(PIN_MQ2, ADC_11db);
}

void loop() {
  // 1 s window at ~200 Hz: vibration = RMS of acceleration magnitude around its mean
  if (adxlOk) {
    const int N = 200;
    float sum = 0, sumSq = 0, peak = 0, lx = 0, ly = 0, lz = 0;
    int n = 0;
    for (int i = 0; i < N; i++) {
      float x, y, z;
      if (adxlRead(x, y, z)) {
        float m = sqrtf(x * x + y * y + z * z);
        sum += m;
        sumSq += m * m;
        lx = x; ly = y; lz = z;
        n++;
      }
      delay(5);
    }
    if (n > 0) {
      float mean = sum / n;
      float rms = sqrtf(fmaxf(sumSq / n - mean * mean, 0));
      Serial.printf("[ADXL345] x=%+.2f y=%+.2f z=%+.2f g | |a|=%.2f g | vib_rms=%.4f g (%d samples)\n",
                    lx, ly, lz, mean, rms, n);
    } else {
      Serial.println("[ADXL345] read failed");
    }
  } else {
    delay(1000);
  }

  uint32_t mv = analogReadMilliVolts(PIN_MQ2);
  Serial.printf("[MQ-2] pin=%u mV -> sensor AO=%.2f V\n", mv, mv * DIVIDER_RATIO / 1000.0f);
}
