package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// oneM2M resource types used by this app (oneM2M TS-0004)
const (
	tyAE        = 2
	tyContainer = 3
	tyCIN       = 4
	tyFlex      = 28
)

// CSE talks to the edge MN-CSE over the HTTP binding (oneM2M TS-0009), using
// structured CSE-relative addresses: <base>/<cse-rn>/<ae-rn>/...
type CSE struct {
	base string // e.g. http://mep1-meep-acme-mn-cse/acme-cse-mn-mep1-sbx3btt489
	rvi  string
	http *http.Client
}

// StatusError is returned when the CSE answers with an unexpected HTTP status.
type StatusError struct {
	Op     string
	Status int
	Body   string
}

func (e *StatusError) Error() string {
	return fmt.Sprintf("%s failed: status %d, body: %s", e.Op, e.Status, e.Body)
}

func NewCSE(base, rvi string) *CSE {
	return &CSE{base: base, rvi: rvi, http: &http.Client{Timeout: 10 * time.Second}}
}

// do sends a oneM2M request on behalf of origin and returns the status code and body.
func (c *CSE) do(method, path, origin string, ty int, body any) (int, []byte, error) {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return 0, nil, fmt.Errorf("failed to encode body: %w", err)
		}
		reader = strings.NewReader(string(b))
	}

	req, err := http.NewRequest(method, c.base+path, reader)
	if err != nil {
		return 0, nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("X-M2M-Origin", origin)
	req.Header.Set("X-M2M-RI", requestID())
	req.Header.Set("X-M2M-RVI", c.rvi)
	req.Header.Set("Accept", "application/json")
	if method == http.MethodPost {
		req.Header.Set("Content-Type", fmt.Sprintf("application/json;ty=%d", ty))
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, respBody, nil
}

// create POSTs a resource under parent. A resource that already exists is
// reused: 409 for a taken resource name, 403 "Originator has already
// registered" for an AE whose originator is known to the CSE.
func (c *CSE) create(op, parent, origin string, ty int, body any) error {
	status, respBody, err := c.do(http.MethodPost, parent, origin, ty, body)
	if err != nil {
		return err
	}
	switch {
	case status == http.StatusCreated, status == http.StatusConflict:
		return nil
	case status == http.StatusForbidden && strings.Contains(string(respBody), "already registered"):
		return nil
	}
	return &StatusError{Op: op, Status: status, Body: string(respBody)}
}

// RegisterDevice creates the device's resource tree, one AE per device:
//
//	<name>            AE   originator C<name>
//	<name>/device     flx  mec:iotDev (same resource MEC033 would create)
//	<name>/telemetry  cnt  sensor samples
//	<name>/link       cnt  link state changes (LTE / VSAT / NONE)
func (c *CSE) RegisterDevice(d Device) error {
	origin, path := d.Originator(), "/"+d.Name
	labels := []string{"ANCHOR", d.Kind}

	ae := map[string]any{"m2m:ae": map[string]any{
		"rn": d.Name, "api": "Nanchor." + d.Kind, "rr": false, "srv": []string{"3", "4", "5"}, "lbl": labels,
	}}
	if err := c.create("create AE "+d.Name, "", origin, tyAE, ae); err != nil {
		return err
	}
	dev := map[string]any{"mec:iotDev": map[string]any{
		"rn": "device", "cnd": "org.onem2m.common.mec.device.iotDevice", "lbl": labels, "devId": d.Name, "enabd": true,
	}}
	if err := c.create("create device "+d.Name, path, origin, tyFlex, dev); err != nil {
		return err
	}
	for _, cnt := range []struct {
		rn  string
		mni int
	}{{"telemetry", 200}, {"link", 50}} {
		body := map[string]any{"m2m:cnt": map[string]any{"rn": cnt.rn, "mni": cnt.mni, "lbl": []string{"ANCHOR", cnt.rn}}}
		if err := c.create("create "+cnt.rn+" "+d.Name, path, origin, tyContainer, body); err != nil {
			return err
		}
	}
	return nil
}

// Push stores one JSON document as a content instance in <device>/<container>.
func (c *CSE) Push(d Device, container string, content []byte) error {
	body := map[string]any{"m2m:cin": map[string]any{"cnf": "application/json:0", "con": string(content)}}
	status, respBody, err := c.do(http.MethodPost, "/"+d.Name+"/"+container, d.Originator(), tyCIN, body)
	if err != nil {
		return err
	}
	if status != http.StatusCreated {
		return &StatusError{Op: "push " + d.Name + "/" + container, Status: status, Body: string(respBody)}
	}
	return nil
}

func requestID() string {
	b := make([]byte, 8)
	_, _ = rand.Read(b)
	return "anchor-fleet-" + hex.EncodeToString(b)
}

// DeleteDevice removes the device's AE (and its subtree) from this CSE. A
// missing AE counts as deleted.
func (c *CSE) DeleteDevice(d Device) error {
	status, respBody, err := c.do(http.MethodDelete, "/"+d.Name, d.Originator(), 0, nil)
	if err != nil {
		return err
	}
	if status != http.StatusOK && status != http.StatusNotFound {
		return &StatusError{Op: "delete AE " + d.Name, Status: status, Body: string(respBody)}
	}
	return nil
}
