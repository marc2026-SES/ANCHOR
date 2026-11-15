// ANCHOR ADN-AE helper: Wi-Fi + oneM2M MQTT binding (TS-0010) to the ship MN-CSE.
//  uplink   : /oneM2M/req/<originator>/<CSE_ID>/json   (responses on /oneM2M/resp/<originator>/<CSE_ID>/json)
//  downlink : /oneM2M/req/<CSE_ID>/<originator>/json   (notifications from the CSE; we answer on .../resp/...)
// Registers AE + containers on every (re)connect (2001, 4105 or 4117 count as OK),
// then each publish() creates one contentInstance with a JSON string in con.
// enableCommands(): adds a "command" container + subscription so the CSE notifies this AE
// whenever someone writes a CIN there (e.g. the dashboard's "zero" button).
// OTA: ArduinoOTA on port 3232, hostname anchor-<aeName>, password OTA_PASS.
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <PubSubClient.h>
#include <functional>
#include <time.h>
#include "anchor_config.h"

#ifndef OTA_PASS
#define OTA_PASS "change-me"
#endif
// Broker address written into the AE's poa, as seen *from the CSE*. It must equal the broker
// address in acme.ini ([mqtt] address) so ACME reuses its existing connection; any other
// spelling makes ACME open a second connection with the same client ID, which the broker kicks.
// The ship Pi's mDNS name. The phone hotspot picks IPs from the MAC and two devices can map to
// the same address (whoever joins first wins), so the nodes look the Pi up by name and only fall
// back to CSE_HOST when mDNS does not answer.
#ifndef CSE_MDNS
#define CSE_MDNS "piking"
#endif
#ifndef POA_BROKER
#define POA_BROKER "127.0.0.1"
#endif

class AnchorNode {
 public:
  using CommandHandler = std::function<void(const String &)>;

  AnchorNode(const char *originator, const char *aeName, const char *appId)
      : orig_(originator), ae_(aeName), api_(appId), mqtt_(net_) {}

  void addContainer(const char *name) {
    if (nCnt_ < 8) cnts_[nCnt_++] = name;
  }

  // Receive commands written to <ae>/command (con is passed to the handler as a string).
  void enableCommands(CommandHandler h) { onCommand_ = h; }

  void begin() {
    reqTopic_ = String("/oneM2M/req/") + orig_ + "/" + CSE_ID + "/json";
    respTopic_ = String("/oneM2M/resp/") + orig_ + "/" + CSE_ID + "/json";
    notifyTopic_ = String("/oneM2M/req/") + CSE_ID + "/" + orig_ + "/json";
    notifyRespTopic_ = String("/oneM2M/resp/") + CSE_ID + "/" + orig_ + "/json";
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    wifiBeginAt_ = millis();
    mqtt_.setBufferSize(2048);
    mqtt_.setKeepAlive(15);
    mqtt_.setCallback([this](char *t, byte *p, unsigned int n) { onMessage(t, p, n); });
    Serial.printf("[NET] Wi-Fi '%s' -> CSE %s:%d as %s\n", WIFI_SSID, CSE_HOST, MQTT_PORT, orig_);
  }

  // Call every loop(); handles Wi-Fi/MQTT reconnect, OTA and AE/CNT registration.
  void loop() {
    if (WiFi.status() != WL_CONNECTED) {
      // The core gives up after a failed association (status 4) or a lost AP; restart the join
      // ourselves every 10 s so a node that drifted out of range rejoins when it comes back.
      if (millis() - wifiBeginAt_ > 10000) {
        wifiBeginAt_ = millis();
        WiFi.disconnect();
        WiFi.begin(WIFI_SSID, WIFI_PASS);
        Serial.printf("[NET] Wi-Fi rejoin (status was %d)\n", WiFi.status());
      }
      if (millis() - lastWifiLog_ > 5000) {
        lastWifiLog_ = millis();
        Serial.printf("[NET] Wi-Fi connecting... status=%d\n", WiFi.status());
      }
      registered_ = false;
      ntpStarted_ = false;
      return;
    }
    if (!otaStarted_) startOta();
    ArduinoOTA.handle();
    if (otaActive_) return;  // keep the radio for the firmware transfer

    // Start SNTP only once the link is up (a request sent before that can stall the first sync).
    // Re-arm every 30 s until synced; chrony on the Pi; timestamps are UTC epoch ms.
    if (!ntpStarted_ || (!nowMs() && millis() - ntpAt_ > 30000)) {
      configTime(0, 0, cseHost_);
      ntpStarted_ = true;
      ntpAt_ = millis();
    }
    if (!mqtt_.connected()) {
      registered_ = false;
      if (millis() - lastTry_ < 3000) return;
      lastTry_ = millis();
      resolveCse();
      Serial.printf("[NET] Wi-Fi OK ip=%s rssi=%d, MQTT connecting to %s...\n", WiFi.localIP().toString().c_str(),
                    WiFi.RSSI(), cseHost_);
      String cid = String(orig_) + "-" + String((uint32_t)ESP.getEfuseMac(), HEX);
      if (!mqtt_.connect(cid.c_str())) {
        Serial.printf("[NET] MQTT failed rc=%d\n", mqtt_.state());
        return;
      }
      mqtt_.subscribe(respTopic_.c_str());
      mqtt_.subscribe(notifyTopic_.c_str());
      Serial.println("[NET] MQTT connected");
    }
    mqtt_.loop();
    if (!registered_ && millis() - lastRegTry_ > 3000) {
      lastRegTry_ = millis();
      registered_ = registerAll();
    }
  }

  bool ready() { return registered_ && mqtt_.connected(); }

  // con: a JSON object as text, e.g. {"roll":1.2}. Adds seq and ts automatically.
  bool publish(const char *cnt, const String &conJson) {
    if (!ready()) return false;
    String con = conJson;
    con.remove(con.length() - 1);  // drop the closing brace
    con += String(",\"seq\":") + (seq_++) + ",\"ts\":" + nowMs() + "}";
    String body = String("{\"op\":1,\"to\":\"") + CSE_NAME + "/" + ae_ + "/" + cnt + "\",\"fr\":\"" + orig_ +
                  "\",\"rqi\":\"" + nextRqi() + "\",\"rvi\":\"3\",\"ty\":4,\"pc\":{\"m2m:cin\":{\"cnf\":\"application/json:0\",\"con\":\"" +
                  escape(con) + "\"}}}";
    sent_++;
    return mqtt_.publish(reqTopic_.c_str(), body.c_str());
  }

  // Low-latency telemetry outside oneM2M (plain MQTT, not stored by the CSE), e.g. 20 Hz attitude
  // for the live 3D view. The oneM2M CIN path stays the system of record.
  bool publishLive(const char *topic, const char *payload) {
    if (!mqtt_.connected() || otaActive_) return false;
    return mqtt_.publish(topic, payload);
  }

  // UTC epoch milliseconds, 0 until NTP has synced.
  static uint64_t nowMs() {
    struct timeval tv;
    gettimeofday(&tv, nullptr);
    if (tv.tv_sec < 1700000000) return 0;
    return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000;
  }

  void printStats() {
    Serial.printf("[STAT] sent=%lu ok=%lu err=%lu last_rsc=%d rssi=%d ntp=%s\n", (unsigned long)sent_,
                  (unsigned long)ok_, (unsigned long)err_, lastRsc_, WiFi.RSSI(), nowMs() ? "synced" : "no");
  }

  int rssi() { return WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0; }

 private:
  const char *orig_, *ae_, *api_;
  const char *cnts_[8];
  int nCnt_ = 0;
  WiFiClient net_;
  PubSubClient mqtt_;
  String reqTopic_, respTopic_, notifyTopic_, notifyRespTopic_;
  CommandHandler onCommand_;
  bool registered_ = false, ntpStarted_ = false, otaStarted_ = false, otaActive_ = false;
  uint32_t ntpAt_ = 0, wifiBeginAt_ = 0, lastRegTry_ = 0;
  uint32_t lastTry_ = 0, lastWifiLog_ = 0, rqiN_ = 0, seq_ = 0;
  uint32_t sent_ = 0, ok_ = 0, err_ = 0;
  int lastRsc_ = 0;
  String waitRqi_;
  int waitRsc_ = 0;

  char cseHost_[16] = CSE_HOST;  // current CSE address (PubSubClient keeps the pointer)

  // mDNS lookup of the ship Pi; MDNS is already running because ArduinoOTA starts it.
  void resolveCse() {
    IPAddress ip = MDNS.queryHost(CSE_MDNS, 1500);
    String found = ip == IPAddress() ? String(CSE_HOST) : ip.toString();
    if (found != cseHost_) {
      Serial.printf("[NET] CSE %s -> %s%s\n", cseHost_, found.c_str(), ip == IPAddress() ? " (mDNS miss, fallback)" : " (mDNS)");
      strlcpy(cseHost_, found.c_str(), sizeof cseHost_);
      ntpStarted_ = false;  // re-point SNTP at the new address
    }
    mqtt_.setServer(cseHost_, MQTT_PORT);
  }

  String nextRqi() { return String(orig_) + "-" + (++rqiN_); }

  void startOta() {
    otaStarted_ = true;
    ArduinoOTA.setHostname((String("anchor-") + ae_).c_str());
    ArduinoOTA.setPassword(OTA_PASS);
    ArduinoOTA.onStart([this]() {
      otaActive_ = true;
      mqtt_.disconnect();
      Serial.println("[OTA] update started");
    });
    ArduinoOTA.onEnd([]() { Serial.println("[OTA] done, rebooting"); });
    ArduinoOTA.onError([this](ota_error_t e) {
      otaActive_ = false;
      Serial.printf("[OTA] error %u\n", e);
    });
    ArduinoOTA.begin();
    Serial.printf("[OTA] ready: anchor-%s @ %s:3232\n", ae_, WiFi.localIP().toString().c_str());
  }

  static String escape(const String &s) {
    String o;
    o.reserve(s.length() + 32);
    for (char c : s) {
      if (c == '"' || c == '\\') o += '\\';
      o += c;
    }
    return o;
  }

  static int findInt(const String &s, const char *key) {
    int i = s.indexOf(key);
    if (i < 0) return -1;
    return s.substring(i + strlen(key)).toInt();
  }

  // Value of the first "key": "..." string in s (handles optional spaces and \" escapes).
  static String findStr(const String &s, const char *key) {
    int i = s.indexOf(String("\"") + key + "\"");
    if (i < 0) return "";
    i = s.indexOf(':', i);
    if (i < 0) return "";
    i = s.indexOf('"', i);
    if (i < 0) return "";
    String o;
    for (int k = i + 1; k < (int)s.length(); k++) {
      char c = s[k];
      if (c == '\\' && k + 1 < (int)s.length()) {
        o += s[++k];
      } else if (c == '"') {
        break;
      } else {
        o += c;
      }
    }
    return o;
  }

  // Notification from the CSE (subscription verification or a new CIN in "command").
  void onNotify(const String &s) {
    String rqi = findStr(s, "rqi");
    String resp = String("{\"rsc\":2000,\"rqi\":\"") + rqi + "\",\"rvi\":\"3\",\"fr\":\"" + orig_ +
                  "\",\"to\":\"/" + CSE_ID + "\"}";
    mqtt_.publish(notifyRespTopic_.c_str(), resp.c_str());
    if (s.indexOf("\"vrq\"") >= 0) {
      Serial.println("[oneM2M] subscription verified");
      return;
    }
    String con = findStr(s, "con");
    if (con.length()) {
      Serial.printf("[CMD] %s\n", con.c_str());
      if (onCommand_) onCommand_(con);
    }
  }

  void onMessage(char *topic, byte *p, unsigned int n) {
    String s;
    s.reserve(n);
    for (unsigned int i = 0; i < n; i++) s += (char)p[i];
    if (notifyTopic_ == topic) {
      onNotify(s);
      return;
    }
    int rsc = findInt(s, "\"rsc\":");
    lastRsc_ = rsc;
    // ACME pretty-prints ("rqi": "x"), so match the quoted rqi value itself (it is unique per request)
    if (waitRqi_.length() && s.indexOf(String("\"") + waitRqi_ + "\"") >= 0) {
      waitRsc_ = rsc;
      return;
    }
    if (rsc >= 2000 && rsc < 3000) {
      ok_++;
    } else {
      err_++;
      Serial.printf("[oneM2M] error response: %s\n", s.substring(0, 200).c_str());
    }
  }

  // Send one request and wait for its response code.
  int request(int op, const String &to, int ty, const String &pc) {
    String rqi = nextRqi();
    String body = String("{\"op\":") + op + ",\"to\":\"" + to + "\",\"fr\":\"" + orig_ + "\",\"rqi\":\"" + rqi +
                  "\",\"rvi\":\"3\"" + (ty ? String(",\"ty\":") + ty : String("")) + ",\"pc\":" + pc + "}";
    waitRqi_ = rqi;
    waitRsc_ = 0;
    mqtt_.publish(reqTopic_.c_str(), body.c_str());
    uint32_t t0 = millis();
    while (!waitRsc_ && millis() - t0 < 5000 && mqtt_.connected()) {
      mqtt_.loop();
      delay(5);
    }
    waitRqi_ = "";
    return waitRsc_;
  }

  static bool okCreate(int rsc) { return rsc == 2001 || rsc == 4105; }

  bool registerAll() {
    String base = String(CSE_NAME);
    String aePath = base + "/" + ae_;
    // Request-reachable with an MQTT point of access, so the CSE can push notifications to us.
    String reach = String("\"rr\":") + (onCommand_ ? "true,\"poa\":[\"mqtt://" POA_BROKER ":" + String(MQTT_PORT) + "\"]"
                                                    : String("false"));
    int rsc = request(1, base, 2,
                      String("{\"m2m:ae\":{\"rn\":\"") + ae_ + "\",\"api\":\"" + api_ + "\"," + reach +
                          ",\"srv\":[\"3\"]}}");
    Serial.printf("[oneM2M] AE %s -> rsc=%d\n", aePath.c_str(), rsc);
    if (rsc == 4117 || rsc == 4105) {
      // already registered (reboot / reconnect): refresh rr/poa in case they changed
      rsc = request(3, aePath, 0, String("{\"m2m:ae\":{") + reach + "}}");
      Serial.printf("[oneM2M] AE update -> rsc=%d\n", rsc);
      if (rsc != 2004) return false;
    } else if (rsc != 2001) {
      return false;
    }
    for (int i = 0; i < nCnt_; i++) {
      rsc = request(1, aePath, 3, String("{\"m2m:cnt\":{\"rn\":\"") + cnts_[i] + "\"}}");
      Serial.printf("[oneM2M] CNT %s -> rsc=%d\n", cnts_[i], rsc);
      if (!okCreate(rsc)) return false;
    }
    if (onCommand_) {
      rsc = request(1, aePath, 3, "{\"m2m:cnt\":{\"rn\":\"command\",\"mni\":50}}");
      Serial.printf("[oneM2M] CNT command -> rsc=%d\n", rsc);
      if (!okCreate(rsc)) return false;
      // net 3 = create of direct child resource; nu = this AE (delivered via its poa)
      rsc = request(1, aePath + "/command", 23,
                    String("{\"m2m:sub\":{\"rn\":\"sub-node\",\"nu\":[\"") + orig_ + "\"],\"enc\":{\"net\":[3]},\"nct\":1}}");
      Serial.printf("[oneM2M] SUB command/sub-node -> rsc=%d\n", rsc);
      if (!okCreate(rsc)) return false;
    }
    Serial.println("[oneM2M] registration done, streaming");
    return true;
  }
};
