package main

import "fmt"

// Device is a UE of the active scenario: a vessel, buoy or camera.
type Device struct {
	Name string
	Kind string // vessel, buoy, cctv or device, from the scenario map icon
}

// Originator is the AE-ID the device registers with (C... = assigned by the app).
func (d Device) Originator() string { return "C" + d.Name }

var iconKinds = map[string]string{
	"ion-android-boat": "vessel",
	"ion-radio-waves":  "buoy",
	"ion-ios-videocam": "cctv",
	"ion-android-car":  "vehicle",
	"ion-android-walk": "pedestrian",
	"ion-iphone":       "device",
}

// element is a node of the scenario tree together with where it sits.
type element struct {
	zone   string
	parent map[string]any // network location holding a physical location
	node   map[string]any
}

// walk visits every network location and physical location of a scenario.
func walk(scn map[string]any, visit func(e element)) {
	deployment, _ := scn["deployment"].(map[string]any)
	for _, dom := range list(deployment["domains"]) {
		for _, zone := range list(dom["zones"]) {
			zoneName, _ := zone["name"].(string)
			for _, nl := range list(zone["networkLocations"]) {
				visit(element{zone: zoneName, node: nl})
				for _, pl := range list(nl["physicalLocations"]) {
					visit(element{zone: zoneName, parent: nl, node: pl})
				}
			}
		}
	}
}

func list(v any) []map[string]any {
	items, _ := v.([]any)
	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		if m, ok := it.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

// Devices lists the scenario's UEs.
func Devices(scn map[string]any) []Device {
	var devices []Device
	walk(scn, func(e element) {
		if e.parent == nil || e.node["type"] != "UE" {
			return
		}
		name, _ := e.node["name"].(string)
		kind := "device"
		if meta, ok := e.node["meta"].(map[string]any); ok {
			if k, ok := iconKinds[fmt.Sprint(meta["display.map.icon"])]; ok {
				kind = k
			}
		}
		devices = append(devices, Device{Name: name, Kind: kind})
	})
	return devices
}

// find returns the element with the given name (a UE or a POA).
func find(scn map[string]any, name string) (element, bool) {
	var found element
	ok := false
	walk(scn, func(e element) {
		if !ok && e.node["name"] == name {
			found, ok = e, true
		}
	})
	return found, ok
}

// ZoneMEPs maps each zone that holds a MEC host (physical location of type
// EDGE) to that host, e.g. kr-busan -> mep1, jp-kyushu -> mep2. Zones without
// a MEC host (the VSAT beam) are left out.
func ZoneMEPs(scn map[string]any) map[string]string {
	out := map[string]string{}
	walk(scn, func(e element) {
		if e.parent == nil || e.node["type"] != "EDGE" {
			return
		}
		if name, _ := e.node["name"].(string); name != "" {
			if _, taken := out[e.zone]; !taken {
				out[e.zone] = name
			}
		}
	})
	return out
}
