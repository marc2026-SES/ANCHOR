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
#include <ArduinoJson.h>
#include <Preferences.h>
#include <esp_system.h>
#include "ReportProfile.h"
#include "anchor_config.h"

#ifndef VESSEL_ID
#define VESSEL_ID "ship-001"
#endif
#ifndef ANCHOR_RAW_MAX_RECORDS
#define ANCHOR_RAW_MAX_RECORDS 20000
#endif
#ifndef ANCHOR_RAW_MAX_BYTES
#define ANCHOR_RAW_MAX_BYTES 5000000
#endif

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

  // BRIDGE and ENGINE support the same report cadence command; CARGO stays fixed.
  void enableReportProfile() { profileEnabled_ = true; }
  uint32_t reportIntervalMs() { return profile_.current(nowMs()); }

  void begin() {
    char boot[24];
    snprintf(boot, sizeof boot, "%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random());
    bootId_ = boot;
    commandPrefs_.begin((String("cmd-") + ae_).c_str(), false);
    String saved = commandPrefs_.getString("state", "{}");
    if (deserializeJson(commandState_, saved)) commandState_.clear();
    profile_.intervalMs = commandState_["intervalMs"] | 1000;
    profile_.untilMs = commandState_["untilMs"] | (uint64_t)0;

    reqTopic_ = String("/oneM2M/req/") + orig_ + "/" + CSE_ID + "/json";
    respTopic_ = String("/oneM2M/resp/") + orig_ + "/" + CSE_ID + "/json";
    notifyTopic_ = String("/oneM2M/req/") + CSE_ID + "/" + orig_ + "/json";
    notifyRespTopic_ = String("/oneM2M/resp/") + CSE_ID + "/" + orig_ + "/json";
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    wifiBeginAt_ = millis();
    mqtt_.setBufferSize(8192);
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
      resultReplayIndex_ = 0;  // persisted device results survive MCU reboot / lost acknowledgements

    }
    mqtt_.loop();
    if (!registered_ && millis() - lastRegTry_ > 3000) {
      lastRegTry_ = millis();
      registered_ = registerAll();
    }
    if (ready()) {
      if (pendingCommand_.length()) {
        String command = pendingCommand_;
        pendingCommand_ = "";
        handleCommand(command);
      }
      replayResults();
      flushQueue();
    }
  }

  bool ready() { return registered_ && mqtt_.connected(); }

  // A bounded RAM outbox retries the identical CIN name and payload until the CSE
  // confirms it. Offline overflow is explicit; power-loss guarantees start at CSE acceptance.
  bool publish(const char *cnt, const String &conJson) {
    JsonDocument con;
    if (deserializeJson(con, conJson) || !con.is<JsonObject>()) return false;
    const uint32_t sequence = seq_++;
    if (queueSize_ >= QUEUE_SIZE) { dropped_++; return false; }
    addIdentity(con, sequence);
    String payload;
    serializeJson(con, payload);
    return enqueue(cnt, String("hw-") + bootId_ + "-" + sequence, payload);
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
    Serial.printf("[STAT] sent=%lu ok=%lu err=%lu queued=%d dropped=%lu last_rsc=%d rssi=%d ntp=%s\n",
                  (unsigned long)sent_, (unsigned long)ok_, (unsigned long)err_, queueSize_,
                  (unsigned long)dropped_, lastRsc_, WiFi.RSSI(), nowMs() ? "synced" : "no");
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
  static constexpr int QUEUE_SIZE = 32;
  struct Pending { String cnt, rn, con; int phase = 0; };
  Pending outbox_[QUEUE_SIZE];
  int queueHead_ = 0, queueSize_ = 0, resultReplayIndex_ = 0;
  String bootId_, txRqi_, pendingCommand_;
  uint32_t txAt_ = 0, retryAt_ = 0, dropped_ = 0;
  bool profileEnabled_ = false, txWaiting_ = false;
  Preferences commandPrefs_;
  JsonDocument commandState_;
  AnchorReportProfile profile_;
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

  void addIdentity(JsonDocument &doc, uint32_t sequence) {
    doc["vesselId"] = VESSEL_ID;
    doc["deviceId"] = ae_;
    doc["bootId"] = bootId_;
    doc["seq"] = sequence;
    doc["ts"] = nowMs();
    doc["uptimeMs"] = millis();
    doc["droppedBefore"] = dropped_;
    doc["eventId"] = String(VESSEL_ID) + ":" + ae_ + ":" + bootId_ + ":" + sequence;
  }

  bool enqueue(const String &cnt, const String &rn, const String &con) {
    for (int i = 0; i < queueSize_; ++i) {
      const auto &p = outbox_[(queueHead_ + i) % QUEUE_SIZE];
      if (p.cnt == cnt && p.rn == rn) return p.con == con;
    }
    if (queueSize_ == QUEUE_SIZE) return false;
    auto &p = outbox_[(queueHead_ + queueSize_) % QUEUE_SIZE];
    p.cnt = cnt; p.rn = rn; p.con = con; p.phase = 0;
    queueSize_++;
    return true;
  }

  void flushQueue() {
    if (!queueSize_ || (retryAt_ && (int32_t)(millis() - retryAt_) < 0)) return;
    if (txWaiting_ && millis() - txAt_ < 3000) return;
    auto &p = outbox_[queueHead_];
    JsonDocument req;
    txRqi_ = nextRqi();
    req["op"] = p.phase == 1 ? 1 : 2;
    req["to"] = String(CSE_NAME) + "/" + ae_ + "/" + p.cnt + (p.phase == 2 ? "/" + p.rn : "");
    req["fr"] = orig_; req["rqi"] = txRqi_; req["rvi"] = "3";
    if (p.phase == 1) {
      req["ty"] = 4;
      req["pc"]["m2m:cin"]["rn"] = p.rn;
      req["pc"]["m2m:cin"]["cnf"] = "application/json:0";
      req["pc"]["m2m:cin"]["con"] = p.con;
    }
    String body; serializeJson(req, body);
    txAt_ = millis(); txWaiting_ = mqtt_.publish(reqTopic_.c_str(), body.c_str());
    if (txWaiting_) sent_++;
  }

  void queueResponse(JsonDocument &response) {
    if (!queueSize_) return;
    txWaiting_ = false;
    auto &p = outbox_[queueHead_];
    const int rsc = response["rsc"] | 5000;
    if (p.phase == 0 && rsc == 2000) {
      JsonObject cnt = response["pc"]["m2m:cnt"];
      // Fail closed if the CSE would silently evict accepted history.
      if (!cnt.isNull() && cnt["mni"].isNull() && cnt["mbs"].isNull() && cnt["mia"].isNull() &&
          (cnt["cni"] | ANCHOR_RAW_MAX_RECORDS) < ANCHOR_RAW_MAX_RECORDS &&
          (cnt["cbs"] | ANCHOR_RAW_MAX_BYTES) + p.con.length() < ANCHOR_RAW_MAX_BYTES) {
        p.phase = 1; return;
      }
      Serial.println("[BACKPRESSURE] raw container full or eviction enabled; keeping pending data");
    } else if (p.phase == 1 && rsc == 4105) {
      p.phase = 2; return;  // lost ACK: compare the actual existing body
    } else if ((p.phase == 1 && rsc == 2001) ||
               (p.phase == 2 && rsc == 2000 && response["pc"]["m2m:cin"]["con"].as<String>() == p.con)) {
      p.cnt = p.rn = p.con = "";
      queueHead_ = (queueHead_ + 1) % QUEUE_SIZE; queueSize_--; ok_++; return;
    } else if (p.phase == 2 && rsc == 4004) {
      p.phase = 0; return;
    }
    err_++; retryAt_ = millis() + 3000;
  }

  void replayResults() {
    JsonArray history = commandState_["history"].as<JsonArray>();
    if (resultReplayIndex_ >= (int)history.size()) return;
    JsonObject row = history[resultReplayIndex_];
    if (enqueue("commandResults", row["rn"].as<String>(), row["result"].as<String>())) resultReplayIndex_++;
  }

  void handleCommand(const String &raw) {
    JsonDocument cmd;
    if (deserializeJson(cmd, raw) || !cmd.is<JsonObject>()) {
      // Existing on-board calibration buttons remain available.
      if (onCommand_) onCommand_(raw);
      return;
    }
    if (!profileEnabled_) return;
    const String id = cmd["commandId"] | "";
    if (!id.length() || id.length() > 64) return;
    String normalized; serializeJson(cmd, normalized);
    JsonArray history = commandState_["history"].as<JsonArray>();
    if (history.isNull()) history = commandState_["history"].to<JsonArray>();
    for (JsonObject row : history) {
      if (row["id"].as<String>() != id) continue;
      if (row["command"].as<String>() != normalized) { Serial.println("[CMD] conflicting ID rejected"); return; }
      enqueue("commandResults", row["rn"].as<String>(), row["result"].as<String>());
      return;  // duplicate never re-applies or extends the original lease
    }
    const uint64_t now = nowMs();
    if (!now) return;  // cannot assess TTL before NTP sync; core will retry
    const uint64_t issued = cmd["issuedAtMs"] | (uint64_t)0;
    const uint64_t expires = cmd["expiresAtMs"] | (uint64_t)0;
    const uint32_t duration = cmd["durationMs"] | 0;
    const uint32_t interval = cmd["intervalMs"] | 0;
    const String target = cmd["target"] | "";
    const uint64_t watermark = commandState_["issuedAtMs"] | (uint64_t)0;
    const String lastId = commandState_["lastId"] | "";
    const char *status = "applied";
    if (!cmd["intervalMs"].is<uint32_t>() || !cmd["durationMs"].is<uint32_t>() ||
        !cmd["issuedAtMs"].is<uint64_t>() || !cmd["expiresAtMs"].is<uint64_t>() ||
        String(cmd["action"] | "") != "setReportProfile" || String(cmd["issuer"] | "") != "shore" ||
        (target != ae_ && target != "reportingGroup") || !AnchorReportProfile::valid(interval, duration) ||
        expires <= issued || expires - issued > 3600000) status = "rejected";
    else if (now >= expires) status = "expired";
    else if (now < issued) return;  // retry after the clock catches up
    else if (issued < watermark || (issued == watermark && id <= lastId)) status = "superseded";
    JsonDocument result;
    result["commandId"] = id; result["status"] = status; result["driver"] = "esp32";
    result["command"].set(cmd.as<JsonObject>());
    result["requestedIntervalMs"] = interval;
    if (String(status) == "applied") {
      commandState_["intervalMs"] = interval;
      commandState_["untilMs"] = now + duration;
      commandState_["issuedAtMs"] = issued;
      commandState_["lastId"] = id;
      result["appliedAtMs"] = now; result["profileUntilMs"] = now + duration;
      result["appliedIntervalMs"] = interval;
    }
    const uint32_t sequence = seq_++;
    addIdentity(result, sequence);
    String resultText; serializeJson(result, resultText);
    String rn = String("hw-") + bootId_ + "-" + sequence;
    if (history.size() >= 8) history.remove(0);
    JsonObject row = history.add<JsonObject>();
    row["id"] = id; row["command"] = normalized; row["result"] = resultText; row["rn"] = rn;
    String saved; serializeJson(commandState_, saved);
    // Persist intent, lease deadline and replayable result together, before applying.
    if (commandPrefs_.putString("state", saved) != saved.length()) {
      Serial.println("[CMD] NVS write failed; profile not applied");
      String old = commandPrefs_.getString("state", "{}");
      deserializeJson(commandState_, old);
      return;
    }
    profile_.intervalMs = commandState_["intervalMs"] | 1000;
    profile_.untilMs = commandState_["untilMs"] | (uint64_t)0;
    resultReplayIndex_ = 0;
    Serial.printf("[CMD] %s %s interval=%lu ms\n", id.c_str(), status, (unsigned long)reportIntervalMs());
  }

  void onMessage(char *topic, byte *p, unsigned int n) {
    JsonDocument msg;
    if (deserializeJson(msg, static_cast<const byte *>(p), n)) { err_++; return; }
    if (notifyTopic_ == topic) {
      JsonDocument resp;
      resp["rsc"] = 2000; resp["rqi"] = msg["rqi"]; resp["rvi"] = "3";
      resp["fr"] = orig_; resp["to"] = String("/") + CSE_ID;
      String text; serializeJson(resp, text);
      mqtt_.publish(notifyRespTopic_.c_str(), text.c_str());
      if (msg["pc"]["m2m:sgn"]["vrq"] | false) return;
      String con = msg["pc"]["m2m:sgn"]["nev"]["rep"]["m2m:cin"]["con"] | "";
      // No nested MQTT calls while PubSubClient is in its callback.
      if (con.length() && !pendingCommand_.length()) pendingCommand_ = con;
      return;
    }
    lastRsc_ = msg["rsc"] | 5000;
    String rqi = msg["rqi"] | "";
    if (waitRqi_.length() && rqi == waitRqi_) { waitRsc_ = lastRsc_; return; }
    if (txWaiting_ && rqi == txRqi_) queueResponse(msg);
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
    String reach = String("\"lbl\":[\"anchor:hardware\",\"device:") + ae_ + "\"]," + String("\"rr\":") + ((onCommand_ || profileEnabled_) ? "true,\"poa\":[\"mqtt://" POA_BROKER ":" + String(MQTT_PORT) + "\"]"
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
      rsc = request(1, aePath, 3, String("{\"m2m:cnt\":{\"rn\":\"") + cnts_[i] + "\",\"lbl\":[\"anchor:sensor\",\"device:" + ae_ + "\",\"sensor:" + cnts_[i] + "\"]}}");
      Serial.printf("[oneM2M] CNT %s -> rsc=%d\n", cnts_[i], rsc);
      if (!okCreate(rsc)) return false;
    }
    if (onCommand_ || profileEnabled_) {
      rsc = request(1, aePath, 3, "{\"m2m:cnt\":{\"rn\":\"command\"}}");
      Serial.printf("[oneM2M] CNT command -> rsc=%d\n", rsc);
      if (!okCreate(rsc)) return false;
      // net 3 = create of direct child resource; nu = this AE (delivered via its poa)
      rsc = request(1, aePath + "/command", 23,
                    String("{\"m2m:sub\":{\"rn\":\"sub-node\",\"nu\":[\"") + orig_ + "\"],\"enc\":{\"net\":[3]},\"nct\":1}}");
      Serial.printf("[oneM2M] SUB command/sub-node -> rsc=%d\n", rsc);
      if (!okCreate(rsc)) return false;
    }
    if (profileEnabled_) {
      rsc = request(1, aePath, 3, "{\"m2m:cnt\":{\"rn\":\"commandResults\"}}");
      if (!okCreate(rsc)) return false;
    }
    Serial.println("[oneM2M] registration done, streaming");
    return true;
  }
};
