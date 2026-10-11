package bff

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"sigs.k8s.io/yaml"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/api"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/auth"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
)

// fakeVerifier はトークンの文字列をそのままロールにする ("view"・"view,deploy")。"bad" は拒む。
type fakeVerifier struct{}

func (fakeVerifier) Verify(_ context.Context, raw string) (*auth.Claims, error) {
	if raw == "bad" {
		return nil, errors.New("bad")
	}
	return &auth.Claims{Subject: "s", PreferredUsername: "alice", Roles: strings.Split(raw, ",")}, nil
}

type seen struct {
	method, path string
	header       http.Header
	body         string
}

func setup(t *testing.T, reply func(w http.ResponseWriter)) (*[]seen, http.Handler) {
	var calls []seen
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		calls = append(calls, seen{r.Method, r.URL.RequestURI(), r.Header.Clone(), string(b)})
		reply(w)
	}))
	t.Cleanup(srv.Close)
	gin.SetMode(gin.TestMode)
	r := gin.New()
	(&Server{Verifier: fakeVerifier{}, APIURL: srv.URL, HTTP: srv.Client(), TemporalUI: "https://temporal", TemporalNS: "gitops-deployer",
		ArgoCD: "https://argocd", Timeout: 10 * time.Second}).Register(r)
	return &calls, r
}

func call(h http.Handler, method, path, token, body string, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	return w
}

func operationReply(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(202)
	_, _ = w.Write([]byte(`{"workflowId":"app-hello","runId":"r1","app":"hello","status":"running","pr":{"number":3,"url":"https://github.com/o/r/pull/3"}}`))
}

func TestAuthAndRoles(t *testing.T) {
	calls, h := setup(t, operationReply)
	for _, c := range []struct {
		method, path, token string
		want                int
	}{
		{"GET", "/api/v1/deployments", "", 401},
		{"GET", "/api/v1/deployments", "bad", 401},
		{"POST", "/api/v1/deployments", "view", 403},
		{"DELETE", "/api/v1/deployments/hello", "view", 403},
		{"GET", "/api/v1/deployments", "deploy", 403},
		{"GET", "/api/v1/deployments/hello", "view", 202},
		{"POST", "/api/v1/deployments", "view,deploy", 202},
	} {
		if w := call(h, c.method, c.path, c.token, "{}", nil); w.Code != c.want {
			t.Errorf("%s %s (%s): %d が返るはず、実際 %d", c.method, c.path, c.token, c.want, w.Code)
		}
	}
	if len(*calls) != 2 {
		t.Errorf("認可を通ったものだけが api に届く: %d", len(*calls))
	}
}

func TestForwardsToAPIWithIdentityAndDeadline(t *testing.T) {
	calls, h := setup(t, operationReply)
	w := call(h, "POST", "/api/v1/deployments", "view,deploy", `{"name":"hello"}`, map[string]string{
		"Content-Type": "application/json", "Idempotency-Key": "k", api.RequestedByHeader: "mallory",
	})
	if w.Code != 202 {
		t.Fatal(w.Code, w.Body)
	}
	got := (*calls)[0]
	if got.path != "/internal/v1/deployments" || got.body != `{"name":"hello"}` || got.header.Get("Idempotency-Key") != "k" {
		t.Errorf("%+v", got)
	}
	if got.header.Get(api.RequestedByHeader) != "alice" {
		t.Errorf("呼び出し元は JWT から作る (送られた値を使わない): %s", got.header.Get(api.RequestedByHeader))
	}
	d, err := time.Parse(time.RFC3339Nano, got.header.Get(server.DeadlineHeader))
	if err != nil || time.Until(d) > 10*time.Second || time.Until(d) < 5*time.Second {
		t.Errorf("期限を api に渡す: %v %v", d, err)
	}
	var body map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	links, _ := body["links"].(map[string]any)
	if links["temporal"] != "https://temporal/namespaces/gitops-deployer/workflows/app-hello/r1" ||
		links["pr"] != "https://github.com/o/r/pull/3" || links["argocd"] != "https://argocd/applications/argocd/deploy-hello" {
		t.Errorf("%v", links)
	}
}

func TestDeadlineFromCallerIsHonoured(t *testing.T) {
	calls, h := setup(t, operationReply)
	soon := time.Now().Add(2 * time.Second).UTC().Format(time.RFC3339Nano)
	call(h, "GET", "/api/v1/deployments/hello", "view", "", map[string]string{server.DeadlineHeader: soon})
	if got := (*calls)[0].header.Get(server.DeadlineHeader); got != soon {
		t.Errorf("呼び出し元の短い期限を引き継ぐ: %s != %s", got, soon)
	}
}

func TestProblemsPassThrough(t *testing.T) {
	_, h := setup(t, func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(409)
		_, _ = w.Write([]byte(`{"title":"Conflict","status":409}`))
	})
	w := call(h, "DELETE", "/api/v1/deployments/hello", "deploy", "", nil)
	if w.Code != 409 || w.Header().Get("Content-Type") != "application/problem+json" || strings.Contains(w.Body.String(), "links") {
		t.Errorf("%d %s %s", w.Code, w.Header(), w.Body)
	}
}

func TestListItemsGetLinks(t *testing.T) {
	_, h := setup(t, func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[{"name":"a","last":{"pr":{"url":"https://github.com/o/r/pull/1"}}}],"nextPageToken":""}`))
	})
	w := call(h, "GET", "/api/v1/deployments?pageSize=5", "view", "", nil)
	if !strings.Contains(w.Body.String(), `"argocd":"https://argocd/applications/argocd/deploy-a"`) || !strings.Contains(w.Body.String(), `"pr":"https://github.com/o/r/pull/1"`) {
		t.Errorf("%s", w.Body)
	}
}

func TestOpenAPIIsValidYAML(t *testing.T) {
	var doc map[string]any
	if err := yaml.Unmarshal(OpenAPI, &doc); err != nil || doc["openapi"] != "3.1.0" {
		t.Fatalf("%v %v", err, doc["openapi"])
	}
}

func TestAuditLogsRequestsWithThePrincipal(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	_, h := setup(t, operationReply)
	call(h, "GET", "/api/v1/deployments", "view", "", nil)
	call(h, "DELETE", "/api/v1/deployments/hello", "view,deploy", "", map[string]string{"Idempotency-Key": "k1"})
	call(h, "POST", "/api/v1/deployments", "view", "{}", nil)
	var got []map[string]any
	for _, line := range bytes.Split(bytes.TrimSpace(buf.Bytes()), []byte("\n")) {
		var m map[string]any
		if json.Unmarshal(line, &m) == nil && m["audit"] == true {
			got = append(got, m)
		}
	}
	if len(got) != 2 {
		t.Fatalf("依頼 (DELETE と、403 になった POST) だけが監査ログに残る。読み取りは残さない: %d 行 %s", len(got), buf.String())
	}
	d := got[0]
	if d["principal"] != "alice" || d["action"] != "DELETE /api/v1/deployments/:name" || d["app"] != "hello" ||
		d["idempotency_key"] != "k1" || d["status"] != float64(202) {
		t.Errorf("誰が・何を・どのアプリに・結果: %v", d)
	}
	if got[1]["status"] != float64(403) {
		t.Errorf("拒んだ依頼も結果ごと残る: %v", got[1])
	}
}
