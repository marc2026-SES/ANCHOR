package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
)

// Server exposes the app over HTTP:
//
//	GET  /health                       liveness check
//	GET  /devices                      link state and store-and-forward counters per device
//	POST /faults/{action}/{target}     inject or clear one fault (see faults.go)
//	POST /faults/run                   play a timeline (JSON [{at, action, target}]; empty body = demo)
//	GET  /faults/demo                  the demo timeline
type Server struct {
	fleet  *Fleet
	faults *Faults
}

func (s *Server) Start(port string) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok\n"))
	})
	mux.HandleFunc("GET /devices", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, s.fleet.Snapshot())
	})
	mux.HandleFunc("GET /faults/demo", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, DemoTimeline)
	})
	mux.HandleFunc("POST /faults/run", s.handleRun)
	mux.HandleFunc("POST /faults/{action}/{target}", s.handleFault)

	go func() {
		log.Printf("HTTP server listening on :%s", port)
		if err := http.ListenAndServe(":"+port, mux); err != nil {
			log.Printf("✗ HTTP server stopped: %v", err)
		}
	}()
}

func (s *Server) handleFault(w http.ResponseWriter, r *http.Request) {
	action, target := r.PathValue("action"), r.PathValue("target")
	if err := s.faults.Apply(action, target); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	log.Printf("⚡ Fault %s %s", action, target)
	writeJSON(w, http.StatusOK, map[string]string{"action": action, "target": target})
}

func (s *Server) handleRun(w http.ResponseWriter, r *http.Request) {
	timeline := DemoTimeline
	if body, _ := io.ReadAll(r.Body); len(body) > 0 {
		if err := json.Unmarshal(body, &timeline); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
	}
	if err := s.faults.Run(timeline); err != nil {
		writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusAccepted, timeline)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
