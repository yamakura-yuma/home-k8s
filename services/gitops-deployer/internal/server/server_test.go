package server

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestHealthReadinessFollowsSetReady(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h := &Health{}
	h.Register(r)
	get := func(path string) int {
		w := httptest.NewRecorder()
		r.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		return w.Code
	}
	if get("/healthz") != 200 || get("/readyz") != 503 {
		t.Fatalf("起動直後: healthz は 200、readyz は依存に繋がるまで 503: %d %d", get("/healthz"), get("/readyz"))
	}
	h.SetReady(true)
	if get("/readyz") != 200 {
		t.Errorf("繋がったら readyz は 200: %d", get("/readyz"))
	}
	h.SetReady(false)
	if get("/readyz") != 503 || get("/healthz") != 200 {
		t.Errorf("止まりかけたら readyz だけ 503 (liveness は落とさない): %d %d", get("/readyz"), get("/healthz"))
	}
}
