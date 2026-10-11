// Package bff は bff の本体 (gin)。外からの唯一の入口 (Istio の Gateway → APISIX → bff)。
//
//   - 認証・認可: auth.Middleware (JWT をもう一度確かめる) と client role (view・deploy)
//   - 監査ログ: 依頼 (POST・DELETE) ごとに、誰が・何を・結果を 1 行の JSON に残す (audit=true、trace_id 付き)
//   - api を Dapr の service invocation で呼ぶ。呼び出しの期限は X-Request-Deadline で api に渡す
//   - Backstage (Scaffolder・カタログのタブ) が使う形に、Temporal UI・PR・Argo CD へのリンクを足して返す
package bff

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/api"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/auth"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/problem"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/server"
)

type Server struct {
	Verifier auth.Verifier
	// APIURL は api の Dapr service invocation の URL の頭 (http://127.0.0.1:3500/v1.0/invoke/api/method)
	APIURL string
	HTTP   *http.Client
	// リンクの頭 (設定 TEMPORAL_UI_URL・ARGOCD_URL)
	TemporalUI string
	TemporalNS string
	ArgoCD     string
	Timeout    time.Duration
}

// 中継するヘッダ (要求)。X-Requested-By は呼び出し元が送っても捨て、JWT から作り直す
var forwardRequest = []string{"Content-Type", "Idempotency-Key", "If-Match"}

// 中継するヘッダ (応答)
var forwardResponse = []string{"Content-Type", "ETag", "Idempotent-Replayed"}

func (s *Server) Register(r *gin.Engine) {
	r.GET("/api/v1/openapi.yaml", func(c *gin.Context) { c.Data(http.StatusOK, "application/yaml", OpenAPI) })
	g := r.Group("/api/v1", server.Deadline(s.Timeout), auth.Middleware(s.Verifier), audit)
	g.GET("/deployments", auth.Require(auth.RoleView), s.proxy("/internal/v1/deployments"))
	g.GET("/deployments/:name", auth.Require(auth.RoleView), s.proxy("/internal/v1/deployments/:name"))
	g.POST("/deployments", auth.Require(auth.RoleDeploy), s.proxy("/internal/v1/deployments"))
	g.DELETE("/deployments/:name", auth.Require(auth.RoleDeploy), s.proxy("/internal/v1/deployments/:name"))
}

// audit は依頼 (POST・DELETE) の監査ログ。読み取りはアクセスログだけに残す。
func audit(c *gin.Context) {
	c.Next()
	if c.Request.Method == http.MethodGet {
		return
	}
	claims := auth.From(c)
	if claims == nil {
		return
	}
	slog.InfoContext(c.Request.Context(), "audit",
		"audit", true, "principal", claims.Principal(), "subject", claims.Subject, "client", claims.AuthorizedParty,
		"action", c.Request.Method+" "+c.FullPath(), "app", c.Param("name"),
		"idempotency_key", c.GetHeader("Idempotency-Key"), "status", c.Writer.Status())
}

func (s *Server) proxy(path string) gin.HandlerFunc {
	return func(c *gin.Context) {
		target := path
		if name := c.Param("name"); name != "" {
			target = "/internal/v1/deployments/" + url.PathEscape(name)
		}
		if c.Request.URL.RawQuery != "" {
			target += "?" + c.Request.URL.RawQuery
		}
		var body io.Reader
		if c.Request.Body != nil {
			b, err := io.ReadAll(io.LimitReader(c.Request.Body, 1<<20))
			if err != nil {
				problem.Write(c, http.StatusBadRequest, "Bad Request", err.Error())
				return
			}
			body = bytes.NewReader(b)
		}
		ctx := c.Request.Context()
		req, err := http.NewRequestWithContext(ctx, c.Request.Method, s.APIURL+target, body)
		if err != nil {
			problem.Write(c, http.StatusInternalServerError, "Internal Server Error", err.Error())
			return
		}
		for _, h := range forwardRequest {
			if v := c.GetHeader(h); v != "" {
				req.Header.Set(h, v)
			}
		}
		req.Header.Set(api.RequestedByHeader, auth.From(c).Principal())
		if d, ok := ctx.Deadline(); ok {
			req.Header.Set(server.DeadlineHeader, d.Format(time.RFC3339Nano))
		}
		resp, err := s.HTTP.Do(req)
		if err != nil {
			status := http.StatusBadGateway
			if ctx.Err() == context.DeadlineExceeded {
				status = http.StatusGatewayTimeout
			}
			problem.Write(c, status, http.StatusText(status), "api を呼べない: "+err.Error())
			return
		}
		defer resp.Body.Close()
		out, _ := io.ReadAll(resp.Body)
		for _, h := range forwardResponse {
			if v := resp.Header.Get(h); v != "" {
				c.Header(h, v)
			}
		}
		if resp.StatusCode < 300 && resp.Header.Get("Content-Type") != problem.ContentType {
			out = s.withLinks(out)
		}
		c.Data(resp.StatusCode, resp.Header.Get("Content-Type"), out)
	}
}

// withLinks は Operation・Deployment・一覧の項目に links (temporal・pr・argocd) を足す。形の分からない JSON はそのまま返す。
func (s *Server) withLinks(b []byte) []byte {
	var v any
	if json.Unmarshal(b, &v) != nil {
		return b
	}
	switch m := v.(type) {
	case map[string]any:
		if items, ok := m["items"].([]any); ok {
			for _, it := range items {
				if im, ok := it.(map[string]any); ok {
					s.addLinks(im)
				}
			}
		} else {
			s.addLinks(m)
		}
	}
	out, err := json.Marshal(v)
	if err != nil {
		return b
	}
	return out
}

func (s *Server) addLinks(m map[string]any) {
	links := map[string]string{}
	op := m
	if o, ok := m["operation"].(map[string]any); ok {
		op = o
	}
	if wf, _ := op["workflowId"].(string); wf != "" && s.TemporalUI != "" {
		u := s.TemporalUI + "/namespaces/" + s.TemporalNS + "/workflows/" + url.PathEscape(wf)
		if run, _ := op["runId"].(string); run != "" {
			u += "/" + url.PathEscape(run)
		}
		links["temporal"] = u
	}
	for _, src := range []map[string]any{op, nested(m, "last")} {
		if pr, ok := src["pr"].(map[string]any); ok {
			if u, _ := pr["url"].(string); u != "" {
				links["pr"] = u
				break
			}
		}
	}
	name, _ := m["name"].(string)
	if name == "" {
		name, _ = m["app"].(string)
	}
	if name != "" && s.ArgoCD != "" {
		links["argocd"] = s.ArgoCD + "/applications/argocd/deploy-" + url.PathEscape(name)
	}
	if len(links) > 0 {
		m["links"] = links
	}
}

func nested(m map[string]any, key string) map[string]any {
	n, _ := m[key].(map[string]any)
	return n
}
