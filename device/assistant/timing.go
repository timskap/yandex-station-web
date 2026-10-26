package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptrace"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Timings contain no prompts, answers, credentials, URLs or account identifiers.
type RequestTiming struct {
	Kind        string `json:"kind"`
	ConnectMS   int64  `json:"connect_ms"`
	FirstByteMS int64  `json:"first_byte_ms"`
	Reused      bool   `json:"reused"`
}
type TurnTiming struct {
	StartedAt    int64           `json:"started_at"`
	Model        string          `json:"model"`
	SpeechModel  string          `json:"speech_model"`
	TextMS       int64           `json:"text_ms"`
	SpeechMS     int64           `json:"speech_ms"`
	FirstAudioMS int64           `json:"first_audio_ms"`
	TotalMS      int64           `json:"total_ms"`
	AnswerChars  int             `json:"answer_chars"`
	AudioBytes   int64           `json:"audio_bytes"`
	Searched     bool            `json:"searched"`
	Outcome      string          `json:"outcome"`
	Network      []RequestTiming `json:"network"`
}
type timingKey struct{}
type turnMetrics struct {
	mu      sync.Mutex
	started time.Time
	value   TurnTiming
}

func (m *turnMetrics) update(f func(*TurnTiming)) { m.mu.Lock(); defer m.mu.Unlock(); f(&m.value) }
func (m *turnMetrics) snapshot() TurnTiming {
	if m == nil {
		return TurnTiming{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	t := m.value
	t.Network = append([]RequestTiming(nil), t.Network...)
	return t
}
func traceRequest(req *http.Request, path string) *http.Request {
	m, _ := req.Context().Value(timingKey{}).(*turnMetrics)
	if m == nil {
		return req
	}
	kind := "text"
	if path == "/audio/speech" {
		kind = "speech"
	}
	index := 0
	m.update(func(t *TurnTiming) { index = len(t.Network); t.Network = append(t.Network, RequestTiming{Kind: kind}) })
	start := time.Now()
	trace := &httptrace.ClientTrace{
		GotConn: func(info httptrace.GotConnInfo) {
			m.update(func(t *TurnTiming) {
				t.Network[index].ConnectMS = time.Since(start).Milliseconds()
				t.Network[index].Reused = info.Reused
			})
		},
		GotFirstResponseByte: func() {
			m.update(func(t *TurnTiming) { t.Network[index].FirstByteMS = time.Since(start).Milliseconds() })
		},
	}
	return req.WithContext(httptrace.WithClientTrace(req.Context(), trace))
}
func (d *Daemon) saveTiming(m *turnMetrics, failure error) {
	m.update(func(t *TurnTiming) {
		t.TotalMS = time.Since(m.started).Milliseconds()
		t.Outcome = "ok"
		if failure != nil {
			t.Outcome = "error"
		}
		if errors.Is(failure, context.Canceled) {
			t.Outcome = "cancelled"
		}
	})
	d.mu.Lock()
	d.recentTimings = append(d.recentTimings, m.snapshot())
	if len(d.recentTimings) > 20 {
		d.recentTimings = d.recentTimings[len(d.recentTimings)-20:]
	}
	b, _ := json.Marshal(d.recentTimings)
	d.mu.Unlock()
	_ = atomicWrite(filepath.Join(d.dir, "timings.json"), b)
}
func (d *Daemon) loadTimings() {
	p := filepath.Join(d.dir, "timings.json")
	if st, err := os.Stat(p); err != nil || st.Size() > 65536 {
		return
	}
	b, err := os.ReadFile(p)
	var saved []TurnTiming
	if err == nil && json.Unmarshal(b, &saved) == nil && len(saved) <= 20 {
		d.recentTimings = saved
	}
}
