// Copy to anchor_config.h (same folder) and fill in. anchor_config.h is the file the sketches use.
#pragma once

#define WIFI_SSID "your-hotspot"
#define WIFI_PASS "your-password"

// Ship Pi (MN-CSE + Mosquitto + chrony) IP on that Wi-Fi, used when the mDNS lookup fails
#define CSE_HOST "192.168.0.10"
#define MQTT_PORT 1883

#define CSE_ID "id-ship"     // MQTT topic part (CSE-ID without the leading slash)
#define CSE_NAME "cse-ship"  // resource name used in "to"

// Password for wireless firmware updates (ArduinoOTA, port 3232). Change it: anyone on the ship LAN
// who knows it can flash the node. Pass the same value to ota.sh (ANCHOR_OTA_PASS).
#define OTA_PASS "change-me"

// The nodes find the ship Pi by mDNS name first and use CSE_HOST only as a fallback
// (a phone hotspot can hand the Pi's address to another device). Default: "piking".
// #define CSE_MDNS "piking"
