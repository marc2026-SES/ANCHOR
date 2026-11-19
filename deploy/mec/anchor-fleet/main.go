package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

// Config holds the app settings. Every field is read from an environment
// variable so the same binary works in the scenario and locally; entrypoint.sh
// derives the defaults from the MEEP_* variables the sandbox injects.
type Config struct {
	Sandbox string // MEEP_SANDBOX_NAME
	MEP     string // MEP_NAME          MEC host this app runs on, e.g. mep1
	AppID   string // MEEP_APP_ID       app instance id, for MEC011 confirm_ready

	CSEURL  string // CSE_URL   home MN-CSE service, e.g. http://mep1-meep-acme-mn-cse
	CSEName string // CSE_NAME  home CSE resource name, e.g. acme-cse-mn-mep1-sbx3btt489
	// TWIN_HANDOVER: keep each twin on the MN-CSE of the MEC host serving the
	// device's zone (mep1 for kr-busan, mep2 for jp-kyushu) and move it on a
	// zone change. Off: every twin stays on the home CSE.
	TwinHandover bool
	RVI          string // CSE_RVI   oneM2M release version indicator

	LocURL        string // LOC_URL          MEC013 Location API base
	AppSupportURL string // APP_SUPPORT_URL  MEC011 app support API base
	CtrlURL       string // CTRL_URL         sandbox-ctrl API base
	GisURL        string // GIS_URL          GIS engine API base

	VSATZones     map[string]bool // VSAT_ZONES     comma-separated zones served by satellite
	LinkInterval  time.Duration   // LINK_INTERVAL  Location API poll period
	PushInterval  time.Duration   // PUSH_INTERVAL  telemetry period per device
	SyncInterval  time.Duration   // SYNC_INTERVAL  inventory / registration refresh period
	BufferSize    int             // BUFFER_SIZE    samples kept per offline device
	FaultAutoplay bool            // FAULT_AUTOPLAY play the demo fault timeline at startup

	HTTPPort string // HTTP_PORT port of the app's HTTP server
}

func loadConfig() Config {
	sandbox := getEnv("MEEP_SANDBOX_NAME", "")
	mep := getEnv("MEP_NAME", "mep1")
	return Config{
		Sandbox: sandbox,
		MEP:     mep,
		AppID:   getEnv("MEEP_APP_ID", ""),

		CSEURL:       getEnv("CSE_URL", fmt.Sprintf("http://%s-meep-acme-mn-cse", mep)),
		CSEName:      getEnv("CSE_NAME", fmt.Sprintf("acme-cse-mn-%s-%s", mep, sandbox)),
		RVI:          getEnv("CSE_RVI", "5"),
		TwinHandover: getEnvBool("TWIN_HANDOVER", true),

		LocURL:        getEnv("LOC_URL", fmt.Sprintf("http://%s-meep-loc-serv/location/v3", mep)),
		AppSupportURL: getEnv("APP_SUPPORT_URL", fmt.Sprintf("http://%s-meep-app-enablement/mec_app_support/v2", mep)),
		CtrlURL:       getEnv("CTRL_URL", "http://meep-sandbox-ctrl/sandbox-ctrl/v1"),
		GisURL:        getEnv("GIS_URL", "http://meep-gis-engine/gis/v1"),

		VSATZones:     parseSet(getEnv("VSAT_ZONES", "zone3")),
		LinkInterval:  getEnvDuration("LINK_INTERVAL", 2*time.Second),
		PushInterval:  getEnvDuration("PUSH_INTERVAL", 10*time.Second),
		SyncInterval:  getEnvDuration("SYNC_INTERVAL", 30*time.Second),
		BufferSize:    getEnvInt("BUFFER_SIZE", 500),
		FaultAutoplay: getEnvBool("FAULT_AUTOPLAY", false),

		HTTPPort: getEnv("HTTP_PORT", "8080"),
	}
}

func main() {
	cfg := loadConfig()
	log.Printf("Configuration: sandbox=%s mep=%s CSE=%s/%s location=%s vsat-zones=%v twin-handover=%v",
		cfg.Sandbox, cfg.MEP, cfg.CSEURL, cfg.CSEName, cfg.LocURL, cfg.VSATZones, cfg.TwinHandover)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	mec := NewMEC(cfg)
	fleet := NewFleet(cfg, mec)
	faults := NewFaults(mec)
	(&Server{fleet: fleet, faults: faults}).Start(cfg.HTTPPort)

	if cfg.AppID != "" {
		if err := mec.ConfirmReady(cfg.AppID); err != nil {
			log.Printf("⚠ MEC011 confirm_ready: %v", err)
		} else {
			log.Printf("✓ MEC011 confirm_ready sent for app %s", cfg.AppID)
		}
	}

	fleet.SyncInventory()
	fleet.UpdateLinks()
	if cfg.FaultAutoplay {
		if err := faults.Run(DemoTimeline); err != nil {
			log.Printf("✗ %v", err)
		}
	}

	linkTicker := time.NewTicker(cfg.LinkInterval)
	pushTicker := time.NewTicker(cfg.PushInterval)
	syncTicker := time.NewTicker(cfg.SyncInterval)
	defer linkTicker.Stop()
	defer pushTicker.Stop()
	defer syncTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			log.Println("Shutting down")
			return
		case <-linkTicker.C:
			fleet.UpdateLinks()
		case <-pushTicker.C:
			fleet.PushTelemetry()
		case <-syncTicker.C:
			fleet.SyncInventory()
		}
	}
}

func getEnv(key, def string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return def
}

func getEnvBool(key string, def bool) bool {
	if v, err := strconv.ParseBool(getEnv(key, "")); err == nil {
		return v
	}
	return def
}

func getEnvInt(key string, def int) int {
	if v, err := strconv.Atoi(getEnv(key, "")); err == nil {
		return v
	}
	return def
}

func getEnvDuration(key string, def time.Duration) time.Duration {
	if v, err := time.ParseDuration(getEnv(key, "")); err == nil && v > 0 {
		return v
	}
	return def
}
