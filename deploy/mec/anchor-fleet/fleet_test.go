package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// fakeCSE records which AEs exist on one MN-CSE.
type fakeCSE struct {
	mu  sync.Mutex
	aes map[string]bool
	cin int
}

func newFakeCSE() (*fakeCSE, *httptest.Server) {
	c := &fakeCSE{aes: map[string]bool{}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.mu.Lock()
		defer c.mu.Unlock()
		parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/") // [cse, ae, ...]
		switch {
		case r.Method == http.MethodPost && len(parts) == 1:
			var body map[string]map[string]any
			json.NewDecoder(r.Body).Decode(&body)
			c.aes[body["m2m:ae"]["rn"].(string)] = true
			w.WriteHeader(http.StatusCreated)
		case r.Method == http.MethodDelete && len(parts) == 2:
			delete(c.aes, parts[1])
			w.WriteHeader(http.StatusOK)
		case len(parts) >= 2 && !c.aes[parts[1]]:
			w.WriteHeader(http.StatusNotFound)
		case r.Method == http.MethodPost && len(parts) == 3:
			if strings.Contains(r.Header.Get("Content-Type"), "ty=4") {
				c.cin++
			}
			w.WriteHeader(http.StatusCreated)
		default:
			w.WriteHeader(http.StatusCreated)
		}
	}))
	return c, srv
}

func TestZoneMEPs(t *testing.T) {
	var scn map[string]any
	json.Unmarshal([]byte(`{"deployment":{"domains":[{"zones":[
	  {"name":"kr-busan","networkLocations":[{"name":"z1","physicalLocations":[{"name":"mep1","type":"EDGE"}]}]},
	  {"name":"jp-kyushu","networkLocations":[{"name":"z2","physicalLocations":[{"name":"mep2","type":"EDGE"}]}]},
	  {"name":"vsat","networkLocations":[{"name":"v","physicalLocations":[{"name":"ship","type":"UE"}]}]}]}]}}`), &scn)
	got := ZoneMEPs(scn)
	if len(got) != 2 || got["kr-busan"] != "mep1" || got["jp-kyushu"] != "mep2" {
		t.Fatalf("ZoneMEPs = %v", got)
	}
}

func TestTwinFollowsZone(t *testing.T) {
	c1, s1 := newFakeCSE()
	defer s1.Close()
	c2, s2 := newFakeCSE()
	defer s2.Close()

	zone := "kr-busan"
	var mu sync.Mutex
	loc := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		json.NewEncoder(w).Encode(map[string]any{"userList": map[string]any{"user": []map[string]any{
			{"address": "ferry", "accessPointId": "poa-" + zone, "zoneId": zone},
		}}})
	}))
	defer loc.Close()
	setZone := func(z string) { mu.Lock(); zone = z; mu.Unlock() }

	cfg := Config{MEP: "mep1", CSEURL: s1.URL, CSEName: "cse1", RVI: "5", TwinHandover: true,
		VSATZones: map[string]bool{"vsat": true}, BufferSize: 10, LocURL: loc.URL}
	f := NewFleet(cfg, NewMEC(cfg))
	f.cses["mep2"] = NewCSE(s2.URL+"/cse2", "5")
	f.zoneMEP = map[string]string{"kr-busan": "mep1", "jp-kyushu": "mep2"}
	f.devices["ferry"] = &DeviceState{Device: Device{Name: "ferry", Kind: "vessel"}, Link: LinkNone}

	where := func() (bool, bool) {
		c1.mu.Lock()
		defer c1.mu.Unlock()
		c2.mu.Lock()
		defer c2.mu.Unlock()
		return c1.aes["ferry"], c2.aes["ferry"]
	}

	f.UpdateLinks()
	if on1, on2 := where(); !on1 || on2 {
		t.Fatalf("in kr-busan: on mep1=%v mep2=%v, want mep1 only", on1, on2)
	}

	setZone("jp-kyushu")
	f.UpdateLinks()
	if on1, on2 := where(); on1 || !on2 {
		t.Fatalf("in jp-kyushu: on mep1=%v mep2=%v, want mep2 only", on1, on2)
	}
	f.PushTelemetry()
	if c2.cin == 0 {
		t.Fatal("telemetry did not reach mep2")
	}

	setZone("vsat") // no MEC host: the twin stays on mep2
	f.UpdateLinks()
	if on1, on2 := where(); on1 || !on2 {
		t.Fatalf("in vsat: on mep1=%v mep2=%v, want mep2 kept", on1, on2)
	}
	if st := f.Snapshot()[0]; st.MEP != "mep2" || !st.Registered {
		t.Fatalf("state = %+v", st)
	}
}
