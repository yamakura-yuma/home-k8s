// Package history は履歴の置き場 (Dapr の state store history、中身は Redis。manifests/dapr.yaml) と、store-activity の Activity。
//
//	app|<名前>  … アプリ 1 つの今の状態と履歴 (新しい順、最大 20 件)
//	apps        … アプリの名前の一覧 (一覧の API のページングに使う)
//
// どちらも ETag で楽観ロックし (読む → 直す → 読んだときの ETag で書く。ぶつかれば読み直す)、同じ run の記録は
// 2 回書かない (Activity の再試行・at-least-once で重複しない)。
package history

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"slices"
	"strconv"

	"go.temporal.io/sdk/activity"

	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/dapr"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/spec"
	"github.com/yamakura-yuma/home-k8s/services/gitops-deployer/internal/workflow"
)

const (
	IndexKey   = "apps"
	maxHistory = 20
	// Topic は完了イベントの Kafka のトピック (Dapr の pub/sub pubsub)
	Topic = "gitops-deployer-events"
)

func AppKey(name string) string { return "app|" + name }

type AppState struct {
	Name     string            `json:"name"`
	Deployed bool              `json:"deployed"`
	Current  *spec.App         `json:"current,omitempty"`
	Last     workflow.Record   `json:"last"`
	History  []workflow.Record `json:"history"`
}

type Store struct{ D *dapr.Client }

// update は key を読み、fn で直し、読んだときの ETag で書く。ぶつかったら読み直す。fn が false なら書かない。
func update[T any](ctx context.Context, d *dapr.Client, key string, fn func(*T) bool) error {
	for range 10 {
		var v T
		etag, _, err := d.GetState(ctx, key, &v)
		if err != nil {
			return err
		}
		if !fn(&v) {
			return nil
		}
		err = d.SaveState(ctx, key, v, etag)
		if errors.Is(err, dapr.ErrConflict) {
			continue
		}
		return err
	}
	return fmt.Errorf("%s の書き込みが 10 回ぶつかった", key)
}

// Append は記録 1 件を足す。同じ run・op の記録がすでにあれば何もしない。
func (s *Store) Append(ctx context.Context, rec workflow.Record) error {
	err := update(ctx, s.D, AppKey(rec.App), func(st *AppState) bool {
		for _, h := range st.History {
			if h.RunID == rec.RunID && h.Op == rec.Op {
				return false
			}
		}
		st.Name = rec.App
		st.Last = rec
		st.History = append([]workflow.Record{rec}, st.History...)
		if len(st.History) > maxHistory {
			st.History = st.History[:maxHistory]
		}
		if rec.Result != workflow.ResultFailed {
			switch rec.Op {
			case spec.OpDeploy:
				st.Deployed, st.Current = true, rec.Spec
			case spec.OpDelete:
				st.Deployed, st.Current = false, nil
			}
		}
		return true
	})
	if err != nil {
		return err
	}
	return update(ctx, s.D, IndexKey, func(names *[]string) bool {
		if slices.Contains(*names, rec.App) {
			return false
		}
		*names = append(*names, rec.App)
		slices.Sort(*names)
		return true
	})
}

// Get はアプリ 1 つの状態と ETag。
func (s *Store) Get(ctx context.Context, name string) (st AppState, etag string, found bool, err error) {
	etag, found, err = s.D.GetState(ctx, AppKey(name), &st)
	return st, etag, found, err
}

// List は名前の順に pageSize 件ずつ返す。pageToken は次の位置 (空なら先頭)。
func (s *Store) List(ctx context.Context, pageToken string, pageSize int) ([]AppState, string, error) {
	var names []string
	if _, _, err := s.D.GetState(ctx, IndexKey, &names); err != nil {
		return nil, "", err
	}
	offset := 0
	if pageToken != "" {
		n, err := strconv.Atoi(pageToken)
		if err != nil || n < 0 || n > len(names) {
			return nil, "", fmt.Errorf("pageToken が読めない: %q", pageToken)
		}
		offset = n
	}
	end := min(offset+pageSize, len(names))
	page := names[offset:end]
	out := []AppState{}
	if len(page) > 0 {
		keys := make([]string, len(page))
		for i, n := range page {
			keys[i] = AppKey(n)
		}
		items, err := s.D.BulkGet(ctx, keys)
		if err != nil {
			return nil, "", err
		}
		byKey := map[string]json.RawMessage{}
		for _, it := range items {
			byKey[it.Key] = it.Data
		}
		for _, k := range keys {
			var st AppState
			if raw := byKey[k]; len(raw) > 0 && json.Unmarshal(raw, &st) == nil {
				out = append(out, st)
			}
		}
	}
	next := ""
	if end < len(names) {
		next = strconv.Itoa(end)
	}
	return out, next, nil
}

// Activities は Task Queue store-activity の Activity。
type Activities struct{ Store *Store }

func (a *Activities) Register(r interface {
	RegisterActivityWithOptions(any, activity.RegisterOptions)
}) {
	r.RegisterActivityWithOptions(a.RecordHistory, activity.RegisterOptions{Name: workflow.ActRecord})
	r.RegisterActivityWithOptions(a.PublishCompleted, activity.RegisterOptions{Name: workflow.ActPublishDone})
}

func (a *Activities) RecordHistory(ctx context.Context, rec workflow.Record) error {
	return a.Store.Append(ctx, rec)
}

// EventID は完了イベントの CloudEvent の id。run ごとに決まった値にするので、Activity の再試行で 2 回出ても
// 受け手が id で重複を捨てられる。
func EventID(rec workflow.Record) string { return rec.WorkflowID + "/" + rec.RunID }

// PublishCompleted は完了イベントを pub/sub に流す。CloudEvent の id・type は Dapr の metadata で決める。
func (a *Activities) PublishCompleted(ctx context.Context, rec workflow.Record) error {
	q := url.Values{
		"metadata.cloudevent.id":   {EventID(rec)},
		"metadata.cloudevent.type": {"io.github.yamakura-yuma.gitops-deployer." + string(rec.Op) + "." + rec.Result},
	}
	return a.Store.D.Publish(ctx, Topic+"?"+q.Encode(), rec)
}
