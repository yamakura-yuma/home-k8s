// Package dapr は Dapr のサイドカー (localhost:3500) の HTTP API の薄いクライアント。アプリは Redis・Kafka の
// クライアントを持たず、state store (statestore、Redis)・pub/sub (pubsub、Kafka)・service invocation だけを使う (合意書)。
// 呼び出しは otelhttp で traceparent を付ける。Dapr はそれを受け継ぎ、pub/sub の CloudEvent の traceparent に入れる。
package dapr

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/retry"
)

// ErrConflict は ETag が合わない (楽観ロックの衝突、first-write で鍵がすでにある)。
var ErrConflict = errors.New("dapr: etag の衝突")

type Client struct {
	Base   string // http://localhost:3500
	Store  string // state store の Component 名
	PubSub string // pub/sub の Component 名
	HTTP   *http.Client
	Retry  retry.Policy
}

// FromEnv は Dapr が入れる DAPR_HTTP_PORT からサイドカーの住所を作る。
func FromEnv() *Client {
	port := os.Getenv("DAPR_HTTP_PORT")
	if port == "" {
		port = "3500"
	}
	return &Client{
		Base:   "http://127.0.0.1:" + port,
		Store:  "statestore",
		PubSub: "pubsub",
		HTTP:   &http.Client{Transport: otelhttp.NewTransport(http.DefaultTransport)},
		Retry:  retry.Default,
	}
}

type StatusError struct {
	Code int
	Body string
}

func (e *StatusError) Error() string { return fmt.Sprintf("dapr: %d %s", e.Code, e.Body) }

// do は 5xx と通信の失敗だけを打ち直す (4xx は打ち直しても変わらない)。
func (c *Client) do(ctx context.Context, method, path string, body any, out any) (http.Header, error) {
	var hdr http.Header
	err := retry.Do(ctx, c.Retry, func(ctx context.Context) error {
		var rd io.Reader
		if body != nil {
			b, err := json.Marshal(body)
			if err != nil {
				return retry.Permanent(err)
			}
			rd = bytes.NewReader(b)
		}
		req, err := http.NewRequestWithContext(ctx, method, c.Base+path, rd)
		if err != nil {
			return retry.Permanent(err)
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := c.HTTP.Do(req)
		if err != nil {
			return err
		}
		defer resp.Body.Close()
		b, _ := io.ReadAll(resp.Body)
		switch {
		case resp.StatusCode == http.StatusConflict:
			return retry.Permanent(ErrConflict)
		case resp.StatusCode >= 500:
			return &StatusError{resp.StatusCode, string(b)}
		case resp.StatusCode >= 400:
			return retry.Permanent(&StatusError{resp.StatusCode, string(b)})
		}
		hdr = resp.Header
		if out != nil && len(b) > 0 {
			if err := json.Unmarshal(b, out); err != nil {
				return retry.Permanent(err)
			}
		}
		return nil
	})
	return hdr, err
}

// GetState は鍵の値と ETag を読む。無ければ found=false。
func (c *Client) GetState(ctx context.Context, key string, out any) (etag string, found bool, err error) {
	var raw json.RawMessage
	hdr, err := c.do(ctx, http.MethodGet, "/v1.0/state/"+c.Store+"/"+url.PathEscape(key), nil, &raw)
	if err != nil {
		return "", false, err
	}
	if len(raw) == 0 {
		return "", false, nil
	}
	return hdr.Get("ETag"), true, json.Unmarshal(raw, out)
}

// SaveState は ETag が今の値と合うときだけ書く (first-write)。etag が空なら、鍵がまだ無いときだけ書く。
func (c *Client) SaveState(ctx context.Context, key string, value any, etag string) error {
	return c.SaveStateTTL(ctx, key, value, etag, 0)
}

// SaveStateTTL は SaveState に有効期限を付ける (0 なら無期限)。
func (c *Client) SaveStateTTL(ctx context.Context, key string, value any, etag string, ttlSeconds int) error {
	item := map[string]any{"key": key, "value": value, "options": map[string]string{"concurrency": "first-write"}}
	if etag != "" {
		item["etag"] = etag
	}
	if ttlSeconds > 0 {
		item["metadata"] = map[string]string{"ttlInSeconds": fmt.Sprint(ttlSeconds)}
	}
	_, err := c.do(ctx, http.MethodPost, "/v1.0/state/"+c.Store, []any{item}, nil)
	return err
}

type BulkItem struct {
	Key  string          `json:"key"`
	Data json.RawMessage `json:"data"`
	ETag string          `json:"etag"`
}

func (c *Client) BulkGet(ctx context.Context, keys []string) ([]BulkItem, error) {
	var items []BulkItem
	_, err := c.do(ctx, http.MethodPost, "/v1.0/state/"+c.Store+"/bulk", map[string]any{"keys": keys}, &items)
	return items, err
}

// Publish は topic に data を出す。Dapr が CloudEvent に包み、id を付け、文脈の traceparent を入れる。
func (c *Client) Publish(ctx context.Context, topic string, data any) error {
	_, err := c.do(ctx, http.MethodPost, "/v1.0/publish/"+c.PubSub+"/"+topic, data, nil)
	return err
}

// InvokeURL は service invocation の URL (http://localhost:3500/v1.0/invoke/<app-id>/method/<path>)。
func (c *Client) InvokeURL(appID, path string) string {
	return c.Base + "/v1.0/invoke/" + appID + "/method/" + path
}
