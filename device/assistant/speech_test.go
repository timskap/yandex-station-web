package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync/atomic"
	"testing"
	"time"
)

func TestGreetingGeneratedOncePerVoice(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		var p map[string]any
		json.NewDecoder(r.Body).Decode(&p)
		if r.URL.Path != "/audio/speech" || p["input"] != greetingText || p["response_format"] != "pcm" {
			t.Error("wrong greeting request")
		}
		w.Write([]byte{1, 0, 2, 0})
	}))
	defer srv.Close()
	d := testDaemon(t, srv.URL)
	os.Remove(greetingPath(d.dir, d.cfg.Voice))
	for i := 0; i < 2; i++ {
		b, err := d.greetingAudio(context.Background(), d.cfg)
		if err != nil || !bytes.Equal(b, []byte{1, 0, 2, 0}) {
			t.Fatal("greeting failed", err)
		}
	}
	if calls.Load() != 1 {
		t.Fatal("greeting regenerated on repeat activation")
	}
	st, err := os.Stat(greetingPath(d.dir, d.cfg.Voice))
	if err != nil || st.Mode().Perm() != 0600 {
		t.Fatal("greeting not saved privately")
	}
	c := d.cfg
	c.Voice = "marin"
	if _, err := d.greetingAudio(context.Background(), c); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 2 {
		t.Fatal("voice change reused the wrong greeting")
	}
}

func TestGreetingFinishesBeforeOpeningMicrophone(t *testing.T) {
	d := testDaemon(t, "")
	defer d.stop()
	playing, release, ready := make(chan struct{}), make(chan struct{}), make(chan struct{})
	d.play = func(ctx context.Context, r io.Reader) error {
		io.Copy(io.Discard, r)
		close(playing)
		select {
		case <-release:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	d.listen = func(_ context.Context, ack func() error, starting func()) error {
		if err := ack(); err != nil {
			return err
		}
		starting()
		close(ready)
		return nil
	}
	d.handleVoice(finalVoice("Спроси GPT"))
	<-playing
	d.handleVoice("onAliceStateChanged: LISTENING, recognizedPhrase=''")
	d.handleVoice(finalVoice("Ага, слушаю"))
	if s := d.status(); s["requests"] != 0 || s["stage"] != "greeting" {
		t.Fatal("greeting was treated as a question")
	}
	select {
	case <-ready:
		t.Fatal("microphone opened before greeting finished")
	default:
	}
	close(release)
	<-ready
	if d.status()["stage"] != "listening" {
		t.Fatal("microphone did not open after greeting")
	}
}

func TestSpeechPlaysBeforeDownloadCompletes(t *testing.T) {
	consumed := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/responses" {
			io.WriteString(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Ответ."}]}]}`)
			return
		}
		w.Write([]byte{1, 0})
		w.(http.Flusher).Flush()
		select {
		case <-consumed:
		case <-time.After(2 * time.Second):
			t.Error("playback waited for full download")
		}
		w.Write([]byte{2, 0})
	}))
	defer srv.Close()
	d := testDaemon(t, srv.URL)
	d.play = func(_ context.Context, r io.Reader) error {
		b := make([]byte, 2)
		if _, err := io.ReadFull(r, b); err != nil {
			return err
		}
		close(consumed)
		_, err := io.Copy(io.Discard, r)
		return err
	}
	if err := d.ask("Вопрос"); err != nil {
		t.Fatal(err)
	}
	waitDone(t, d)
	if d.err != "" {
		t.Fatal(d.err)
	}
}

func TestStreamingCancellation(t *testing.T) {
	playing := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		if r.URL.Path == "/responses" {
			io.WriteString(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Ответ."}]}]}`)
			return
		}
		w.Write([]byte{0, 0})
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer srv.Close()
	d := testDaemon(t, srv.URL)
	d.play = func(_ context.Context, r io.Reader) error {
		b := make([]byte, 2)
		if _, err := io.ReadFull(r, b); err != nil {
			return err
		}
		close(playing)
		_, err := io.Copy(io.Discard, r)
		return err
	}
	if err := d.ask("Вопрос"); err != nil {
		t.Fatal(err)
	}
	<-playing
	d.stop()
	if d.busy || d.err != "" {
		t.Fatal("stream cancellation failed")
	}
}

func TestInvalidPCMRejected(t *testing.T) {
	for _, data := range [][]byte{nil, {1}, {1, 2, 3, 4, 5, 6}} {
		p := &pcmStream{reader: bytes.NewReader(data), limit: 4}
		io.Copy(io.Discard, p)
		if p.validate() == nil {
			t.Fatal("invalid or oversized PCM accepted")
		}
	}
}

func TestGreetingTailKeepsPadding(t *testing.T) {
	b := make([]byte, 48000)
	b[10000], b[10001] = 0xff, 0x7f
	if got := trimGreetingTail(b); len(got) != 10002+1920 || got[10000] != 0xff {
		t.Fatal("greeting silence trim lost audible data or padding")
	}
}

func TestWebSearchAndFastModelPayload(t *testing.T) {
	c := defaults()
	p := responsePayload(c, []turn{{"user", "Привет"}})
	if p["tools"] != nil || p["reasoning"].(map[string]string)["effort"] != "none" {
		t.Fatal("default payload")
	}
	c.WebSearch = true
	p = responsePayload(c, []turn{{"user", "Свежие новости"}})
	if p["tools"].([]map[string]any)[0]["type"] != "web_search" || p["tool_choice"] != "auto" || p["store"] != false {
		t.Fatal("search payload")
	}
	d := testDaemon(t, "")
	if err := d.update([]byte(`{"enabled":true,"web_search":true}`), false); err != nil {
		t.Fatal(err)
	}
	if err := d.update([]byte(`{"enabled":true,"model":"custom-model"}`), false); err != nil {
		t.Fatal(err)
	}
	saved, err := readConfig(d.dir)
	if err != nil || !saved.WebSearch {
		t.Fatal("search setting lost on partial update")
	}
}

func TestSearchSourcesAndSpokenCitations(t *testing.T) {
	b := []byte(`{"output":[{"type":"web_search_call"},{"type":"message","content":[{"annotations":[{"type":"url_citation","url":"https://example.com/news","title":"Новости"},{"type":"url_citation","url":"https://example.com/news"},{"type":"url_citation","url":"javascript:alert(1)"},{"type":"url_citation","url":"https://user:secret@example.com"}]}]}]}`)
	s, searched := responseSources(b)
	if !searched || len(s) != 1 || s[0].Title != "Новости" {
		t.Fatal("citation parsing or URL filtering failed")
	}
	if got := speechText("По данным [сайта](https://example.com), всё хорошо. citeturn0search0"); got != "По данным сайта, всё хорошо." {
		t.Fatal(got)
	}
}
