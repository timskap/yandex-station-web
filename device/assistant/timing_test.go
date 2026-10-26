package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestHTTPConnectionsReusedAndReplacedWithRoutingConfig(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { io.Copy(io.Discard, r.Body); io.WriteString(w, "ok") }))
	defer srv.Close()
	d := testDaemon(t, srv.URL)
	defer d.closeClient()
	m := &turnMetrics{started: time.Now()}
	ctx := context.WithValue(context.Background(), timingKey{}, m)
	for i := 0; i < 2; i++ {
		if _, err := d.post(ctx, d.cfg, "/responses", map[string]string{"input": "private-question"}, 32); err != nil {
			t.Fatal(err)
		}
	}
	n := m.snapshot().Network
	if len(n) != 2 || n[0].Reused || !n[1].Reused {
		t.Fatal("connection was not reused", n)
	}
	old := d.clientFor(d.cfg)
	c := d.cfg
	c.VPNDNS = "9.9.9.9"
	if d.clientFor(c) == old {
		t.Fatal("routing config reused old connection pool")
	}
	d.closeClient()
	if d.client != nil {
		t.Fatal("pool was not cleared")
	}
}

func TestTimingsArePrivateBoundedAndSurviveRestart(t *testing.T) {
	d := testDaemon(t, "")
	for i := 0; i < 22; i++ {
		m := &turnMetrics{started: time.Now(), value: TurnTiming{StartedAt: int64(i), Model: "test", FirstAudioMS: 12}}
		d.saveTiming(m, nil)
	}
	file := filepath.Join(d.dir, "timings.json")
	b, err := os.ReadFile(file)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), d.cfg.APIKey) || strings.Contains(string(b), "input") {
		t.Fatal("timing log exposed content")
	}
	st, _ := os.Stat(file)
	if st.Mode().Perm() != 0600 {
		t.Fatal("timing log permissions")
	}
	var saved []TurnTiming
	if json.Unmarshal(b, &saved) != nil || len(saved) != 20 || saved[0].StartedAt != 2 || saved[19].Outcome != "ok" {
		t.Fatal("unbounded or invalid timing log")
	}
	other := &Daemon{dir: d.dir}
	other.loadTimings()
	if len(other.recentTimings) != 20 {
		t.Fatal("lost history on restart")
	}
}

func TestSpeechModePreservesTextModelAndSearch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var p map[string]any
		json.NewDecoder(r.Body).Decode(&p)
		if r.URL.Path == "/responses" {
			if p["model"] != "gpt-6-luna" || p["tools"] == nil {
				t.Error("speech mode changed text model or search")
			}
			io.WriteString(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Ответ."}]}]}`)
		} else {
			if p["model"] != "tts-1" || p["response_format"] != "pcm" {
				t.Error("wrong fast speech payload")
			}
			w.Write([]byte{0, 0, 1, 0})
		}
	}))
	defer srv.Close()
	d := testDaemon(t, srv.URL)
	defer d.closeClient()
	if err := d.update([]byte(`{"enabled":true,"web_search":true,"speech_model":"tts-1"}`), false); err != nil {
		t.Fatal(err)
	}
	if err := d.ask("Вопрос"); err != nil {
		t.Fatal(err)
	}
	waitDone(t, d)
	if d.err != "" || d.status()["speech_model"] != "tts-1" {
		t.Fatal("speech mode failed", d.err)
	}
	if err := d.update([]byte(`{"enabled":true,"voice":"marin"}`), false); err == nil {
		t.Fatal("unsupported voice accepted")
	}
	saved, err := readConfig(d.dir)
	if err != nil || saved.SpeechModel != "tts-1" || saved.Voice != "coral" || !saved.WebSearch {
		t.Fatal("config was not preserved")
	}
}
