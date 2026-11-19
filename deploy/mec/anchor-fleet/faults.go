package main

import (
	"fmt"
	"log"
	"sort"
	"sync"
	"time"
)

// Faults injects failures into the running scenario through sandbox events.
//
//	radio-down <ue>   device failure: the UE drops off the network (red on the map)
//	radio-up <ue>     repair it
//	poa-down <poa>    base station outage: 100% packet loss through the POA
//	poa-up <poa>      restore the POA's original network characteristics
//	storm <poa>       bad weather on a (VSAT) POA: 1.5 s latency, 0.5/0.2 Mbps, 30% loss
//	clear <poa>       same as poa-up
//
// radio-down switches the UE's wirelessType to "wifi". The scenario has no
// Wi-Fi POA, so the GIS mobility automation finds nothing in range and keeps
// the UE disconnected; a plain MOBILITY "DISCONNECTED" event is undone by the
// automation within a second.
type Faults struct {
	mec *MEC

	mu       sync.Mutex
	original map[string]map[string]any // POA name -> netChar before any fault
	running  bool
}

// Step is one entry of a fault timeline.
type Step struct {
	At     int    `json:"at"` // seconds after the timeline starts
	Action string `json:"action"`
	Target string `json:"target"`
}

// DemoTimeline is played by POST /faults/run without a body.
var DemoTimeline = []Step{
	{0, "radio-down", "buoy-05"},      // buoy battery dies
	{60, "storm", "vsat-poa-2"},       // weather turns on the southeast VSAT beam
	{120, "radio-down", "fishing-02"}, // fishing boat terminal fails
	{180, "clear", "vsat-poa-2"},
	{240, "poa-down", "yeongdo-poa-4g-1"}, // coastal base station outage
	{300, "poa-up", "yeongdo-poa-4g-1"},
	{330, "radio-up", "fishing-02"},
	{360, "radio-up", "buoy-05"},
}

var (
	stormNetChar  = map[string]any{"latency": 1500, "latencyVariation": 300, "throughputDl": 0.5, "throughputUl": 0.2, "packetLoss": 30}
	outageNetChar = map[string]any{"packetLoss": 100}
)

func NewFaults(mec *MEC) *Faults {
	return &Faults{mec: mec, original: map[string]map[string]any{}}
}

// Apply runs one fault action against a target.
func (f *Faults) Apply(action, target string) error {
	switch action {
	case "radio-down":
		return f.setRadio(target, "wifi")
	case "radio-up":
		return f.setRadio(target, "4g")
	case "poa-down":
		return f.setNetChar(target, outageNetChar)
	case "storm":
		return f.setNetChar(target, stormNetChar)
	case "poa-up", "clear":
		return f.restoreNetChar(target)
	}
	return fmt.Errorf("unknown action %q", action)
}

// Run plays a timeline in the background; only one timeline runs at a time.
func (f *Faults) Run(timeline []Step) error {
	f.mu.Lock()
	if f.running {
		f.mu.Unlock()
		return fmt.Errorf("a fault timeline is already running")
	}
	f.running = true
	f.mu.Unlock()

	steps := append([]Step(nil), timeline...)
	sort.SliceStable(steps, func(i, j int) bool { return steps[i].At < steps[j].At })
	go func() {
		defer func() { f.mu.Lock(); f.running = false; f.mu.Unlock() }()
		start := time.Now()
		for _, s := range steps {
			time.Sleep(time.Until(start.Add(time.Duration(s.At) * time.Second)))
			if err := f.Apply(s.Action, s.Target); err != nil {
				log.Printf("✗ Fault +%ds %s %s: %v", s.At, s.Action, s.Target, err)
				continue
			}
			log.Printf("⚡ Fault +%ds %s %s", s.At, s.Action, s.Target)
		}
	}()
	return nil
}

func (f *Faults) setRadio(ue, wirelessType string) error {
	scn, err := f.mec.ActiveScenario()
	if err != nil {
		return err
	}
	e, ok := find(scn, ue)
	if !ok || e.parent == nil {
		return fmt.Errorf("UE %s not found", ue)
	}
	pl := map[string]any{}
	for k, v := range e.node {
		pl[k] = v
	}
	pl["wirelessType"] = wirelessType
	pl["connected"] = wirelessType != "wifi"
	// Re-send a moving UE at its current position, not its scenario start point
	if loc, err := f.mec.CurrentLocation(ue); err == nil && loc != nil {
		geo := map[string]any{}
		if g, ok := pl["geoData"].(map[string]any); ok {
			for k, v := range g {
				geo[k] = v
			}
		}
		geo["location"] = loc
		pl["geoData"] = geo
	}
	return f.mec.SendEvent("SCENARIO-UPDATE", map[string]any{"eventScenarioUpdate": map[string]any{
		"action": "MODIFY",
		"node":   map[string]any{"type": "UE", "parent": e.parent["name"], "nodeDataUnion": map[string]any{"physicalLocation": pl}},
	}})
}

func (f *Faults) setNetChar(poa string, netChar map[string]any) error {
	scn, err := f.mec.ActiveScenario()
	if err != nil {
		return err
	}
	e, ok := find(scn, poa)
	if !ok || e.parent != nil {
		return fmt.Errorf("POA %s not found", poa)
	}
	f.mu.Lock()
	if _, saved := f.original[poa]; !saved {
		orig, _ := e.node["netChar"].(map[string]any)
		f.original[poa] = orig
	}
	f.mu.Unlock()
	return f.sendNetChar(poa, e.node["type"], netChar)
}

func (f *Faults) restoreNetChar(poa string) error {
	f.mu.Lock()
	orig, saved := f.original[poa]
	delete(f.original, poa)
	f.mu.Unlock()
	if !saved {
		return nil // no fault applied to this POA
	}
	scn, err := f.mec.ActiveScenario()
	if err != nil {
		return err
	}
	e, ok := find(scn, poa)
	if !ok {
		return fmt.Errorf("POA %s not found", poa)
	}
	// netChar fields are omitempty: zero the fault's fields the original did not set
	netChar := map[string]any{"latency": 0, "latencyVariation": 0, "throughputDl": 1000, "throughputUl": 1000, "packetLoss": 0}
	for k, v := range orig {
		netChar[k] = v
	}
	return f.sendNetChar(poa, e.node["type"], netChar)
}

func (f *Faults) sendNetChar(poa string, poaType any, netChar map[string]any) error {
	return f.mec.SendEvent("NETWORK-CHARACTERISTICS-UPDATE", map[string]any{"eventNetworkCharacteristicsUpdate": map[string]any{
		"elementName": poa, "elementType": poaType, "netChar": netChar,
	}})
}
