package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func finalVoice(text string) string {
	return "onAliceStateChanged: BUSY, recognizedPhrase='" + text + "'"
}

func activateVoice(t *testing.T, d *Daemon) {
	t.Helper()
	started := make(chan struct{})
	d.listen = func(_ context.Context, starting func()) error { starting(); close(started); return nil }
	d.handleVoice(finalVoice("Алиса, спроси ГПТ"))
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("trigger did not start native recognition")
	}
}

func TestTwoStepVoiceAndDirectQuestion(t *testing.T) {
	var calls, plays atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path == "/responses" {
			io.WriteString(w, `{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"Ответ."}]}]}`)
		} else if r.URL.Path == "/audio/speech" {
			w.Write([]byte{0, 0})
		} else {
			t.Error("unexpected API request")
		}
	}))
	defer srv.Close()
	d := testDaemon(t, srv.URL)
	defer d.stop()
	d.play = func(context.Context, []byte) error { plays.Add(1); return nil }

	d.handleVoice(finalVoice("Обычная команда Алисе"))
	activateVoice(t, d)
	if s := d.status(); s["listening"] != true || s["requests"] != 0 || calls.Load() != 0 {
		t.Fatal("activation must wait for a question without calling OpenAI")
	}
	d.handleVoice(finalVoice("Результат предыдущего распознавания"))
	if d.status()["requests"] != 0 {
		t.Fatal("accepted a final result before native listening started")
	}
	d.handleVoice("onAliceStateChanged: LISTENING, recognizedPhrase=''")
	d.handleVoice("onRequestStateChanged: LISTENING, recognizedPhrase='Сколько будет'")
	if d.status()["requests"] != 0 {
		t.Fatal("forwarded a partial hypothesis")
	}
	const question = "Сколько будет 3.14 * 2?"
	d.handleVoice(finalVoice(question))
	d.handleVoice(finalVoice(question))
	waitDone(t, d)
	if calls.Load() != 2 || plays.Load() != 1 || d.status()["listening"] != false {
		t.Fatal("separate question was lost, duplicated or left listening armed")
	}
	if len(d.history) != 2 || d.history[0].Content != question {
		t.Fatal("question text was changed")
	}
	d.handleVoice(finalVoice("Включи музыку"))
	if d.status()["requests"] != 1 {
		t.Fatal("forwarded an unrelated utterance after the question")
	}
	d.handleVoice(finalVoice("Спроси ChatGPT, Почему небо голубое?"))
	waitDone(t, d)
	if calls.Load() != 4 || plays.Load() != 2 || d.history[2].Content != "Почему небо голубое?" {
		t.Fatal("single-utterance mode regressed")
	}
}

func TestListeningEndsWithoutForwardingLaterSpeech(t *testing.T) {
	for _, how := range []string{"stop", "timeout", "native idle", "disabled", "voice cancel"} {
		t.Run(how, func(t *testing.T) {
			d := testDaemon(t, "")
			defer d.stop()
			activateVoice(t, d)
			d.handleVoice("onAliceStateChanged: LISTENING, recognizedPhrase=''")
			switch how {
			case "stop":
				d.stop()
			case "timeout":
				d.expireListening(time.Now().Add(time.Minute))
			case "native idle":
				d.handleVoice("onAliceStateChanged: IDLE, recognizedPhrase=''")
			case "disabled":
				if err := d.update([]byte(`{"enabled":false}`), false); err != nil {
					t.Fatal(err)
				}
			case "voice cancel":
				d.handleVoice(finalVoice("Отмена."))
			}
			if d.status()["listening"] != false {
				t.Fatal("still listening")
			}
			d.handleVoice("onAliceStateChanged: LISTENING, recognizedPhrase=''")
			d.handleVoice(finalVoice("Обычная команда Алисе"))
			if d.status()["requests"] != 0 {
				t.Fatal("accepted speech after listening ended")
			}
		})
	}
}

func TestActivationGates(t *testing.T) {
	for _, missing := range []string{"enabled", "key", "vpn"} {
		t.Run(missing, func(t *testing.T) {
			d := testDaemon(t, "")
			defer d.stop()
			switch missing {
			case "enabled":
				d.cfg.Enabled = false
			case "key":
				d.cfg.APIKey = ""
			case "vpn":
				d.cfg.VPNEnabled = true
			}
			d.handleVoice(finalVoice("Спроси GPT"))
			if s := d.status(); s["listening"] != false || s["requests"] != 0 {
				t.Fatal("activated despite missing prerequisite")
			}
		})
	}
}

func TestStoppedListeningCannotBeRearmedByLateCLI(t *testing.T) {
	d := testDaemon(t, "")
	finished := make(chan struct{})
	d.listen = func(ctx context.Context, starting func()) error {
		<-ctx.Done()
		starting()
		close(finished)
		return errors.New("cancelled command")
	}
	d.handleVoice(finalVoice("Спроси GPT"))
	d.handleVoice("onAliceStateChanged: LISTENING, recognizedPhrase=''")
	d.handleVoice(finalVoice("Реплика до запуска нового распознавания"))
	if d.status()["requests"] != 0 {
		t.Fatal("old listening event armed the next question")
	}
	d.stop()
	<-finished
	if s := d.status(); s["listening"] != false || s["error"] != "" {
		t.Fatal("late callback changed stopped state")
	}
}
