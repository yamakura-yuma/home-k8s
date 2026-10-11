package gitops

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/retry"
)

type staticToken struct{ tok string }

func (s *staticToken) Token(context.Context) (string, error) { return s.tok, nil }
func (s *staticToken) Invalidate()                           {}

// fakeApp は POST /app/installations/<id>/access_tokens。App の JWT を公開鍵で確かめ、連番のトークンを返す。
type fakeApp struct {
	pub    *rsa.PublicKey
	issued atomic.Int32
	bodies []map[string]any
	claims []jwt.Claims
}

func (f *fakeApp) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" || r.URL.Path != "/app/installations/42/access_tokens" {
		w.WriteHeader(404)
		return
	}
	tok, err := jwt.ParseSigned(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "), []jose.SignatureAlgorithm{jose.RS256})
	var c jwt.Claims
	if err == nil {
		err = tok.Claims(f.pub, &c)
	}
	if err != nil {
		w.WriteHeader(401)
		_, _ = w.Write([]byte(`{"message":"A JSON web token could not be decoded"}`))
		return
	}
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	f.bodies, f.claims = append(f.bodies, body), append(f.claims, c)
	n := f.issued.Add(1)
	w.WriteHeader(201)
	_ = json.NewEncoder(w).Encode(map[string]any{"token": "ghs_" + string(rune('0'+n)), "expires_at": time.Date(2026, 1, 1, 13, 0, 0, 0, time.UTC)})
}

func newAppToken(t *testing.T) (*AppToken, *fakeApp, *time.Time) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "private-key.pem")
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	if err := os.WriteFile(path, pemBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	f := &fakeApp{pub: &key.PublicKey}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	now := time.Date(2026, 1, 1, 12, 0, 0, 0, time.UTC)
	return &AppToken{API: srv.URL, AppID: "1234", InstallationID: "42", KeyFile: path, Repo: "home-k8s", HTTP: srv.Client(),
		Now: func() time.Time { return now }}, f, &now
}

func TestAppTokenSignsAsTheAppAndScopesToOneRepo(t *testing.T) {
	a, f, now := newAppToken(t)
	tok, err := a.Token(context.Background())
	if err != nil || tok != "ghs_1" {
		t.Fatalf("token = %q, %v", tok, err)
	}
	c := f.claims[0]
	if c.Issuer != "1234" || c.IssuedAt.Time().After(*now) || c.Expiry.Time().Sub(*now) > 10*time.Minute {
		t.Fatalf("claims = %+v", c)
	}
	// installation がほかのリポジトリ・権限を持っていても、取るトークンは home-k8s の contents・pull_requests だけ
	b, _ := json.Marshal(f.bodies[0])
	if string(b) != `{"permissions":{"contents":"write","pull_requests":"write"},"repositories":["home-k8s"]}` {
		t.Fatalf("body = %s", b)
	}
}

func TestAppTokenIsReusedUntilFiveMinutesBeforeExpiry(t *testing.T) {
	a, f, now := newAppToken(t)
	ctx := context.Background()
	_, _ = a.Token(ctx)
	*now = now.Add(54 * time.Minute) // 12:54 (切れるのは 13:00)
	if tok, _ := a.Token(ctx); tok != "ghs_1" || f.issued.Load() != 1 {
		t.Fatalf("reused = %q, issued %d", tok, f.issued.Load())
	}
	*now = now.Add(2 * time.Minute) // 12:56
	if tok, _ := a.Token(ctx); tok != "ghs_2" {
		t.Fatalf("refreshed = %q", tok)
	}
}

func TestAppTokenFailsOnABadKey(t *testing.T) {
	a, _, _ := newAppToken(t)
	if err := os.WriteFile(a.KeyFile, []byte("not a pem"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Token(context.Background()); err == nil || !strings.Contains(err.Error(), "PEM") {
		t.Fatalf("err = %v", err)
	}
}

// GitHub が 401 を返したら (installation token が取り消された等)、トークンを取り直して 1 度だけ打ち直す。
func TestCallRefreshesTheTokenOnceOn401(t *testing.T) {
	a, f, _ := newAppToken(t)
	var seen []string
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Header.Get("Authorization"))
		if r.Header.Get("Authorization") == "Bearer ghs_1" {
			w.WriteHeader(401)
			return
		}
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer api.Close()
	g := &GitHub{API: api.URL, Token: a, HTTP: api.Client(), Retry: retry.Policy{Attempts: 1}}
	var out map[string]bool
	if err := g.rest(context.Background(), "GET", "/x", nil, &out); err != nil || !out["ok"] {
		t.Fatalf("out = %v, err = %v", out, err)
	}
	if strings.Join(seen, ",") != "Bearer ghs_1,Bearer ghs_2" || f.issued.Load() != 2 {
		t.Fatalf("seen = %v, issued %d", seen, f.issued.Load())
	}
}
