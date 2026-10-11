// Package auth は bff の認証と認可。APISIX が Keycloak (realm home-k8s) の JWT を検証したうえで、bff も署名・issuer・
// audience・期限を確かめ直し (二重の確認。APISIX を迂回した呼び出しも通さない)、client gitops-deployer の client role で認可する。
//
//	view   … 一覧と状態を読む
//	deploy … デプロイと削除を依頼する
//
// ロールはアクセストークンの resource_access.gitops-deployer.roles に入る (Keycloak の既定の roles scope)。
package auth

import (
	"context"
	"log/slog"
	"net/http"
	"slices"
	"strings"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/gin-gonic/gin"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/problem"
)

const (
	RoleView   = "view"
	RoleDeploy = "deploy"
)

type Claims struct {
	Subject           string `json:"sub"`
	AuthorizedParty   string `json:"azp"`
	PreferredUsername string `json:"preferred_username"`
	ResourceAccess    map[string]struct {
		Roles []string `json:"roles"`
	} `json:"resource_access"`
	Roles []string `json:"-"`
}

// Principal は監査ログと履歴に残す呼び出し元 (人なら preferred_username、client credentials なら service-account-<client>)。
func (c *Claims) Principal() string {
	if c.PreferredUsername != "" {
		return c.PreferredUsername
	}
	return c.Subject
}

type Verifier interface {
	Verify(ctx context.Context, raw string) (*Claims, error)
}

// OIDC は JWKS の鍵で JWT を確かめる。鍵は初回と未知の kid のときに取りに行き、キャッシュする。
type OIDC struct {
	v        *oidc.IDTokenVerifier
	audience string
}

func NewOIDC(ctx context.Context, issuer, jwksURL, audience string) *OIDC {
	ks := oidc.NewRemoteKeySet(ctx, jwksURL)
	return NewWithKeySet(issuer, ks, audience)
}

func NewWithKeySet(issuer string, ks oidc.KeySet, audience string) *OIDC {
	// go-oidc の IDTokenVerifier はアクセストークンにも使える (iss・aud・exp・署名を見る)。nonce は見ない
	return &OIDC{v: oidc.NewVerifier(issuer, ks, &oidc.Config{ClientID: audience}), audience: audience}
}

func (o *OIDC) Verify(ctx context.Context, raw string) (*Claims, error) {
	tok, err := o.v.Verify(ctx, raw)
	if err != nil {
		return nil, err
	}
	var c Claims
	if err := tok.Claims(&c); err != nil {
		return nil, err
	}
	c.Roles = c.ResourceAccess[o.audience].Roles
	return &c, nil
}

const claimsKey = "auth.claims"

// Middleware は Authorization: Bearer を確かめる。無い・読めない・期限切れ・署名違い・宛先違いはどれも 401。
func Middleware(v Verifier) gin.HandlerFunc {
	return func(c *gin.Context) {
		raw, ok := strings.CutPrefix(c.GetHeader("Authorization"), "Bearer ")
		if !ok || raw == "" {
			c.Header("WWW-Authenticate", `Bearer realm="gitops-deployer"`)
			problem.Write(c, http.StatusUnauthorized, "Unauthorized", "Bearer トークンが無い")
			return
		}
		claims, err := v.Verify(c.Request.Context(), raw)
		if err != nil {
			slog.WarnContext(c.Request.Context(), "トークンを拒んだ", "error", err.Error())
			c.Header("WWW-Authenticate", `Bearer realm="gitops-deployer", error="invalid_token"`)
			problem.Write(c, http.StatusUnauthorized, "Unauthorized", "トークンを確かめられない")
			return
		}
		c.Set(claimsKey, claims)
		c.Next()
	}
}

// Require は client role を要求する。持っていなければ 403。
func Require(role string) gin.HandlerFunc {
	return func(c *gin.Context) {
		claims := From(c)
		if claims == nil || !slices.Contains(claims.Roles, role) {
			problem.Write(c, http.StatusForbidden, "Forbidden", "ロール "+role+" が要る")
			return
		}
		c.Next()
	}
}

func From(c *gin.Context) *Claims {
	v, _ := c.Get(claimsKey)
	claims, _ := v.(*Claims)
	return claims
}
