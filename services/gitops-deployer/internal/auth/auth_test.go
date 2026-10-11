package auth

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/gin-gonic/gin"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

const issuer = "https://keycloak.example/realms/home-k8s"

type keys struct {
	priv *rsa.PrivateKey
	ks   oidc.KeySet
}

func newKeys(t *testing.T) keys {
	priv, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return keys{priv, &oidc.StaticKeySet{PublicKeys: []crypto.PublicKey{&priv.PublicKey}}}
}

func (k keys) token(t *testing.T, mutate func(map[string]any)) string {
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: k.priv}, nil)
	if err != nil {
		t.Fatal(err)
	}
	claims := map[string]any{
		"iss": issuer, "aud": []string{"gitops-deployer", "account"}, "sub": "u1", "azp": "gitops-deployer",
		"preferred_username": "service-account-gitops-deployer",
		"exp":                time.Now().Add(time.Minute).Unix(), "iat": time.Now().Unix(),
		"resource_access": map[string]any{"gitops-deployer": map[string]any{"roles": []string{"view"}}},
	}
	if mutate != nil {
		mutate(claims)
	}
	raw, err := jwt.Signed(signer).Claims(claims).Serialize()
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func router(v Verifier) *gin.Engine {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	g := r.Group("/", Middleware(v))
	g.GET("/read", Require(RoleView), func(c *gin.Context) { c.String(200, From(c).Principal()) })
	g.POST("/write", Require(RoleDeploy), func(c *gin.Context) { c.String(200, "ok") })
	return r
}

func call(r http.Handler, method, path, authz string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, nil)
	if authz != "" {
		req.Header.Set("Authorization", authz)
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

func TestTokens(t *testing.T) {
	k := newKeys(t)
	other := newKeys(t)
	r := router(NewWithKeySet(issuer, k.ks, "gitops-deployer"))
	cases := []struct {
		name   string
		method string
		path   string
		authz  string
		want   int
	}{
		{"トークン無し", "GET", "/read", "", 401},
		{"Bearer でない", "GET", "/read", "Basic Zm9vOmJhcg==", 401},
		{"壊れたトークン", "GET", "/read", "Bearer not-a-jwt", 401},
		{"別の鍵の署名", "GET", "/read", "Bearer " + other.token(t, nil), 401},
		{"期限切れ", "GET", "/read", "Bearer " + k.token(t, func(c map[string]any) { c["exp"] = time.Now().Add(-time.Hour).Unix() }), 401},
		{"別の issuer", "GET", "/read", "Bearer " + k.token(t, func(c map[string]any) { c["iss"] = "https://evil.example" }), 401},
		{"宛先が違う", "GET", "/read", "Bearer " + k.token(t, func(c map[string]any) { c["aud"] = "account" }), 401},
		{"view で読む", "GET", "/read", "Bearer " + k.token(t, nil), 200},
		{"view で書く", "POST", "/write", "Bearer " + k.token(t, nil), 403},
		{"deploy で書く", "POST", "/write", "Bearer " + k.token(t, func(c map[string]any) {
			c["resource_access"] = map[string]any{"gitops-deployer": map[string]any{"roles": []string{"view", "deploy"}}}
		}), 200},
		{"別の client の deploy では書けない", "POST", "/write", "Bearer " + k.token(t, func(c map[string]any) {
			c["resource_access"] = map[string]any{"other": map[string]any{"roles": []string{"deploy"}}}
		}), 403},
	}
	for _, c := range cases {
		w := call(r, c.method, c.path, c.authz)
		if w.Code != c.want {
			t.Errorf("%s: %d が返るはず、実際 %d %s", c.name, c.want, w.Code, w.Body)
		}
		if w.Code == 401 && w.Header().Get("Content-Type") != "application/problem+json" {
			t.Errorf("%s: problem+json でない: %s", c.name, w.Header().Get("Content-Type"))
		}
	}
	if w := call(r, "GET", "/read", "Bearer "+k.token(t, nil)); w.Body.String() != "service-account-gitops-deployer" {
		t.Errorf("principal: %s", w.Body)
	}
}
