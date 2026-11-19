package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// MEC wraps the sandbox REST APIs this app uses:
//   - MEC013 Location API on its MEC host, for the devices' live attachment
//   - MEC011 app support, to confirm the app is ready
//   - sandbox-ctrl and the GIS engine, for the device inventory and fault injection
type MEC struct {
	locURL        string // e.g. http://mep1-meep-loc-serv/location/v3
	appSupportURL string // e.g. http://mep1-meep-app-enablement/mec_app_support/v2
	ctrlURL       string // e.g. http://meep-sandbox-ctrl/sandbox-ctrl/v1
	gisURL        string // e.g. http://meep-gis-engine/gis/v1
	http          *http.Client
}

func NewMEC(cfg Config) *MEC {
	return &MEC{
		locURL:        cfg.LocURL,
		appSupportURL: cfg.AppSupportURL,
		ctrlURL:       cfg.CtrlURL,
		gisURL:        cfg.GisURL,
		http:          &http.Client{Timeout: 10 * time.Second},
	}
}

func (m *MEC) call(method, url string, body, out any) error {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, url, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := m.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("%s %s: status %d, body: %s", method, url, resp.StatusCode, respBody)
	}
	if out != nil && len(respBody) > 0 {
		return json.Unmarshal(respBody, out)
	}
	return nil
}

// LocationUser is one entry of the MEC013 users query (only the fields used here).
type LocationUser struct {
	Address       string `json:"address"`
	AccessPointID string `json:"accessPointId"`
	ZoneID        string `json:"zoneId"`
	LocationInfo  struct {
		Latitude  []float64 `json:"latitude"`
		Longitude []float64 `json:"longitude"`
	} `json:"locationInfo"`
}

// Users returns the devices the MEC Location API currently sees attached,
// keyed by address. A device missing from the map has no network attachment.
func (m *MEC) Users() (map[string]LocationUser, error) {
	var resp struct {
		UserList struct {
			User []LocationUser `json:"user"`
		} `json:"userList"`
	}
	if err := m.call(http.MethodGet, m.locURL+"/queries/users", nil, &resp); err != nil {
		return nil, err
	}
	users := make(map[string]LocationUser, len(resp.UserList.User))
	for _, u := range resp.UserList.User {
		users[u.Address] = u
	}
	return users, nil
}

// ConfirmReady tells the MEC platform (MEC011) the app instance is up.
func (m *MEC) ConfirmReady(appID string) error {
	url := fmt.Sprintf("%s/applications/%s/confirm_ready", m.appSupportURL, appID)
	return m.call(http.MethodPost, url, map[string]string{"indication": "READY"}, nil)
}

// ActiveScenario returns the scenario running in this sandbox.
func (m *MEC) ActiveScenario() (map[string]any, error) {
	var scn map[string]any
	err := m.call(http.MethodGet, m.ctrlURL+"/active", nil, &scn)
	return scn, err
}

// SendEvent posts a sandbox event (MOBILITY, SCENARIO-UPDATE, NETWORK-CHARACTERISTICS-UPDATE, ...).
func (m *MEC) SendEvent(eventType string, event map[string]any) error {
	event["name"] = "anchor-fleet"
	event["type"] = eventType
	return m.call(http.MethodPost, m.ctrlURL+"/events/"+eventType, event, nil)
}

// CurrentLocation returns the GIS engine's current GeoJSON location of an asset.
func (m *MEC) CurrentLocation(asset string) (map[string]any, error) {
	var resp struct {
		Location map[string]any `json:"location"`
	}
	err := m.call(http.MethodGet, m.gisURL+"/geodata/"+asset, nil, &resp)
	return resp.Location, err
}
