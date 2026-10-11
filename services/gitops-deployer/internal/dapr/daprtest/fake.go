// Package daprtest は単体試験用の偽の Dapr サイドカー (state store・publish・service invocation の一部)。
// ETag は書くたびに増える版で、Dapr の first-write と同じく、ETag が合わなければ 409、ETag 無しは鍵が無いときだけ書ける。
package daprtest

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/retry"
)

type Fake struct {
	mu        sync.Mutex
	Data      map[string]json.RawMessage
	Ver       map[string]int
	TTL       map[string]string
	Conflicts int // 次の n 回の書き込みを、ほかの誰かが先に書いたことにする
	Published []string
	// Invoke は service invocation (/v1.0/invoke/<app>/method/...) を受ける (無ければ 404)
	Invoke http.Handler
}

func New(t *testing.T) (*Fake, *dapr.Client) {
	f := &Fake{Data: map[string]json.RawMessage{}, Ver: map[string]int{}, TTL: map[string]string{}}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return f, &dapr.Client{Base: srv.URL, Store: "statestore", PubSub: "pubsub", HTTP: srv.Client(),
		Retry: retry.Policy{Attempts: 2, Base: time.Millisecond, Max: time.Millisecond}}
}

func (f *Fake) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if strings.HasPrefix(r.URL.Path, "/v1.0/invoke/") {
		if f.Invoke == nil {
			w.WriteHeader(404)
			return
		}
		f.Invoke.ServeHTTP(w, r)
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	b, _ := io.ReadAll(r.Body)
	switch {
	case r.Method == "GET" && strings.HasPrefix(r.URL.Path, "/v1.0/state/statestore/"):
		key := strings.TrimPrefix(r.URL.Path, "/v1.0/state/statestore/")
		if v, ok := f.Data[key]; ok {
			w.Header().Set("ETag", strconv.Itoa(f.Ver[key]))
			_, _ = w.Write(v)
			return
		}
		w.WriteHeader(204)
	case r.Method == "POST" && r.URL.Path == "/v1.0/state/statestore":
		var items []struct {
			Key      string            `json:"key"`
			Value    json.RawMessage   `json:"value"`
			ETag     string            `json:"etag"`
			Metadata map[string]string `json:"metadata"`
		}
		_ = json.Unmarshal(b, &items)
		for _, it := range items {
			if f.Conflicts > 0 {
				f.Conflicts--
				f.Ver[it.Key]++
				w.WriteHeader(409)
				return
			}
			_, exists := f.Data[it.Key]
			if (it.ETag == "" && exists) || (it.ETag != "" && it.ETag != strconv.Itoa(f.Ver[it.Key])) {
				w.WriteHeader(409)
				return
			}
			f.Data[it.Key] = it.Value
			f.Ver[it.Key]++
			f.TTL[it.Key] = it.Metadata["ttlInSeconds"]
		}
		w.WriteHeader(204)
	case r.Method == "POST" && r.URL.Path == "/v1.0/state/statestore/bulk":
		var req struct {
			Keys []string `json:"keys"`
		}
		_ = json.Unmarshal(b, &req)
		out := []map[string]any{}
		for _, k := range req.Keys {
			out = append(out, map[string]any{"key": k, "data": f.Data[k], "etag": strconv.Itoa(f.Ver[k])})
		}
		_ = json.NewEncoder(w).Encode(out)
	case r.Method == "POST" && strings.HasPrefix(r.URL.Path, "/v1.0/publish/pubsub/"):
		f.Published = append(f.Published, r.URL.Path+"?"+r.URL.RawQuery+" "+string(b))
		w.WriteHeader(204)
	default:
		w.WriteHeader(404)
	}
}
