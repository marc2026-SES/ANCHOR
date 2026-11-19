package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math"
	"math/rand/v2"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

// Link types derived from the zone a device is attached to
const (
	LinkLTE  = "LTE"  // coastal LTE-M, zones with a MEC host
	LinkVSAT = "VSAT" // offshore satellite beam
	LinkNone = "NONE" // no attachment: shadow spot or device failure
)

// DeviceState is what the app knows about one device. While the device has no
// link its samples are held in Buffer and sent, oldest first, when it is back.
type DeviceState struct {
	Device
	Registered bool      `json:"registered"`
	MEP        string    `json:"mep"` // MEC host whose MN-CSE holds the twin
	Link       string    `json:"link"`
	Zone       string    `json:"zone,omitempty"`
	POA        string    `json:"poa,omitempty"`
	Lat        float64   `json:"lat,omitempty"`
	Lon        float64   `json:"lon,omitempty"`
	LinkSince  time.Time `json:"linkSince"`
	Seq        int       `json:"seq"`
	Sent       int       `json:"sent"`
	Buffered   int       `json:"buffered"`
	Replayed   int       `json:"replayed"`
	Dropped    int       `json:"dropped"`
	buffer     [][]byte
}

// Fleet keeps every scenario device registered as a twin on an edge MN-CSE,
// tracks its link through the MEC Location API and pushes its telemetry.
// With TwinHandover the twin lives on the MN-CSE of the MEC host serving the
// device's zone and moves when the device crosses into another MEC zone.
type Fleet struct {
	cfg Config
	mec *MEC

	mu      sync.Mutex
	devices map[string]*DeviceState
	cses    map[string]*CSE   // per MEC host
	zoneMEP map[string]string // zone -> MEC host, from the active scenario
}

func NewFleet(cfg Config, mec *MEC) *Fleet {
	home := NewCSE(cfg.CSEURL+"/"+cfg.CSEName, cfg.RVI)
	return &Fleet{cfg: cfg, mec: mec, devices: map[string]*DeviceState{},
		cses: map[string]*CSE{cfg.MEP: home}, zoneMEP: map[string]string{}}
}

// cseFor returns the client for a MEC host's MN-CSE (platform naming:
// http://<mep>-meep-acme-mn-cse/acme-cse-mn-<mep>-<sandbox>).
func (f *Fleet) cseFor(mep string) *CSE {
	f.mu.Lock()
	defer f.mu.Unlock()
	if mep == "" {
		mep = f.cfg.MEP
	}
	if c, ok := f.cses[mep]; ok {
		return c
	}
	c := NewCSE(fmt.Sprintf("http://%s-meep-acme-mn-cse/acme-cse-mn-%s-%s", mep, mep, f.cfg.Sandbox), f.cfg.RVI)
	f.cses[mep] = c
	return c
}

// cseName is the CSE resource name of a MEC host, for logs.
func (f *Fleet) cseName(mep string) string {
	if mep == f.cfg.MEP {
		return f.cfg.CSEName
	}
	return fmt.Sprintf("acme-cse-mn-%s-%s", mep, f.cfg.Sandbox)
}

// targetMEP picks the MEC host for a device in zone. Zones without a MEC host
// (VSAT, no link) keep the twin where it is, so it does not bounce.
// Caller holds f.mu.
func (f *Fleet) targetMEP(st *DeviceState, zone string) string {
	if !f.cfg.TwinHandover {
		return f.cfg.MEP
	}
	if mep, ok := f.zoneMEP[zone]; ok {
		return mep
	}
	if st.MEP != "" {
		return st.MEP
	}
	return f.cfg.MEP
}

// Snapshot returns a copy of all device states, sorted by name.
func (f *Fleet) Snapshot() []DeviceState {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]DeviceState, 0, len(f.devices))
	for _, d := range f.devices {
		out = append(out, *d)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// SyncInventory adds the active scenario's devices and registers any device
// not yet on the CSE.
func (f *Fleet) SyncInventory() {
	scn, err := f.mec.ActiveScenario()
	if err != nil {
		log.Printf("✗ Inventory: %v", err)
		return
	}
	zm := ZoneMEPs(scn)
	f.mu.Lock()
	if len(zm) > 0 {
		f.zoneMEP = zm
	}
	for _, d := range Devices(scn) {
		if _, ok := f.devices[d.Name]; !ok {
			f.devices[d.Name] = &DeviceState{Device: d, Link: LinkNone, LinkSince: time.Now()}
		}
	}
	f.mu.Unlock()
	f.registerPending()
}

// registerPending creates the twin of every device not yet on its CSE. A
// device still without a MEC host (never seen in a MEC zone) goes to the
// home CSE.
func (f *Fleet) registerPending() {
	for _, st := range f.Snapshot() {
		if st.Registered {
			continue
		}
		mep := st.MEP
		if mep == "" {
			mep = f.cfg.MEP
		}
		if err := f.cseFor(mep).RegisterDevice(st.Device); err != nil {
			log.Printf("✗ Register %s on %s: %v", st.Name, mep, err)
			continue
		}
		f.mu.Lock()
		cur := f.devices[st.Name]
		if cur.MEP == "" || cur.MEP == mep {
			cur.MEP, cur.Registered = mep, true
			log.Printf("✓ Registered %s (%s) on %s", st.Name, st.Kind, f.cseName(mep))
		}
		f.mu.Unlock()
	}
}

// moveTwin re-creates a twin on the new MEC host's CSE and removes it from
// the old one (MEC zone handover of the twin).
func (f *Fleet) moveTwin(d Device, from, to string) {
	if err := f.cseFor(to).RegisterDevice(d); err != nil {
		log.Printf("✗ Move %s %s → %s: %v", d.Name, from, to, err)
		return // stays unregistered; registerPending retries on `to`
	}
	f.mu.Lock()
	if st := f.devices[d.Name]; st.MEP == to {
		st.Registered = true
	}
	f.mu.Unlock()
	if from != "" {
		if err := f.cseFor(from).DeleteDevice(d); err != nil {
			log.Printf("⚠ Remove old twin %s on %s: %v", d.Name, from, err)
		}
	}
	log.Printf("⇄ twin %s: %s → %s", d.Name, f.cseName(from), f.cseName(to))
}

// UpdateLinks reads the MEC Location API and records every link change in
// the device's link container; a device coming back replays its buffer.
func (f *Fleet) UpdateLinks() {
	users, err := f.mec.Users()
	if err != nil {
		log.Printf("✗ Location API: %v", err)
		return
	}

	type move struct {
		d        Device
		from, to string
	}
	var moves []move

	f.mu.Lock()
	var changed []*DeviceState
	for name, st := range f.devices {
		link, zone, poa := LinkNone, "", ""
		if u, ok := users[name]; ok {
			zone, poa = u.ZoneID, u.AccessPointID
			link = LinkLTE
			if f.cfg.VSATZones[zone] {
				link = LinkVSAT
			}
			if len(u.LocationInfo.Latitude) > 0 && len(u.LocationInfo.Longitude) > 0 {
				st.Lat, st.Lon = u.LocationInfo.Latitude[0], u.LocationInfo.Longitude[0]
			}
		}
		if link != st.Link || poa != st.POA {
			log.Printf("↔ %s: %s %s → %s %s", name, st.Link, st.POA, link, poa)
			st.Link, st.Zone, st.POA, st.LinkSince = link, zone, poa, time.Now()
			changed = append(changed, st)
		}
		if to := f.targetMEP(st, zone); to != st.MEP {
			if st.Registered {
				moves = append(moves, move{st.Device, st.MEP, to})
			}
			st.MEP, st.Registered = to, false
		}
	}
	f.mu.Unlock()

	for _, m := range moves {
		f.moveTwin(m.d, m.from, m.to)
	}
	f.registerPending()
	for _, st := range changed {
		f.recordLink(st)
		f.flush(st)
	}
}

// recordLink writes the link state into <device>/link. The edge app reaches
// the CSE even when the device itself is offline, so NONE is recorded too.
func (f *Fleet) recordLink(st *DeviceState) {
	f.mu.Lock()
	doc, _ := json.Marshal(map[string]any{
		"link": st.Link, "zone": st.Zone, "poa": st.POA, "buffered": len(st.buffer),
		"timestamp": st.LinkSince.UTC().Format(time.RFC3339),
	})
	d, mep := st.Device, st.MEP
	f.mu.Unlock()
	if err := f.cseFor(mep).Push(d, "link", doc); err != nil {
		f.handlePushError(st, err)
	}
}

// PushTelemetry takes one sample per device: sent if the device has a link,
// buffered otherwise (store-and-forward).
func (f *Fleet) PushTelemetry() {
	for _, st := range f.Snapshot() {
		f.mu.Lock()
		cur := f.devices[st.Name]
		cur.Seq++
		sample, _ := json.Marshal(sampleFor(cur))
		online := cur.Link != LinkNone
		if !online {
			if len(cur.buffer) >= f.cfg.BufferSize {
				cur.buffer = cur.buffer[1:]
				cur.Dropped++
			}
			cur.buffer = append(cur.buffer, sample)
			cur.Buffered = len(cur.buffer)
		}
		f.mu.Unlock()

		if online {
			f.flush(cur)
			f.send(cur, sample, false)
		}
	}
}

// flush replays a device's buffered samples in order once it has a link.
func (f *Fleet) flush(st *DeviceState) {
	f.mu.Lock()
	if st.Link == LinkNone || len(st.buffer) == 0 {
		f.mu.Unlock()
		return
	}
	pending := st.buffer
	st.buffer, st.Buffered = nil, 0
	f.mu.Unlock()

	log.Printf("⟳ %s back on %s: replaying %d buffered samples", st.Name, st.Link, len(pending))
	for i, s := range pending {
		if !f.send(st, s, true) {
			f.mu.Lock()
			st.buffer = append(pending[i:], st.buffer...)
			st.Buffered = len(st.buffer)
			f.mu.Unlock()
			return
		}
	}
}

func (f *Fleet) send(st *DeviceState, sample []byte, replay bool) bool {
	f.mu.Lock()
	mep := st.MEP
	f.mu.Unlock()
	if err := f.cseFor(mep).Push(st.Device, "telemetry", sample); err != nil {
		f.handlePushError(st, err)
		return false
	}
	f.mu.Lock()
	st.Sent++
	if replay {
		st.Replayed++
	}
	f.mu.Unlock()
	return true
}

// handlePushError re-registers a device whose resources disappeared from the
// CSE (e.g. the CSE pod restarted).
func (f *Fleet) handlePushError(st *DeviceState, err error) {
	log.Printf("✗ %v", err)
	var se *StatusError
	if errors.As(err, &se) && se.Status == http.StatusNotFound {
		f.mu.Lock()
		st.Registered = false
		f.mu.Unlock()
	}
}

// sampleFor builds a plausible sensor reading for the device kind.
func sampleFor(st *DeviceState) map[string]any {
	s := map[string]any{
		"device":    st.Name,
		"seq":       st.Seq,
		"timestamp": time.Now().UTC().Format(time.RFC3339),
		"link":      st.Link,
	}
	if st.Lat != 0 || st.Lon != 0 {
		s["lat"], s["lon"] = st.Lat, st.Lon
	}
	switch st.Kind {
	case "vessel":
		s["engineTempC"] = round(70+rand.Float64()*25, 1)
		s["gasPpm"] = round(rand.Float64()*20, 1)
		if rand.IntN(40) == 0 {
			s["gasPpm"] = round(60+rand.Float64()*60, 1) // occasional gas alarm
		}
	case "buoy":
		s["waveHeightM"] = round(0.2+rand.Float64()*2.8, 2)
		s["waterTempC"] = round(14+rand.Float64()*8, 1)
		s["batteryPct"] = 100 - st.Seq%100
	case "cctv":
		s["motion"] = rand.IntN(5) == 0
		s["fps"] = 15
	}
	return s
}

func round(v float64, places int) float64 {
	p := math.Pow(10, float64(places))
	return math.Round(v*p) / p
}

func parseSet(csv string) map[string]bool {
	set := map[string]bool{}
	for _, v := range strings.Split(csv, ",") {
		if v = strings.TrimSpace(v); v != "" {
			set[v] = true
		}
	}
	return set
}
