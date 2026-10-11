// Package retry は Temporal の外の呼び出し (GitHub・Dapr の HTTP) の再試行。指数バックオフに full jitter を掛ける
// (同時に失敗した呼び出しが同じ間隔で一斉に打ち直さないように)。Temporal の Activity の再試行は RetryPolicy に任せる。
package retry

import (
	"context"
	"errors"
	"math/rand/v2"
	"time"
)

type Policy struct {
	Attempts int           // 最初の 1 回を含む回数
	Base     time.Duration // 1 回目の待ちの上限
	Max      time.Duration // 待ちの上限の上限
}

var Default = Policy{Attempts: 4, Base: 200 * time.Millisecond, Max: 3 * time.Second}

type permanentError struct{ err error }

func (e permanentError) Error() string { return e.err.Error() }
func (e permanentError) Unwrap() error { return e.err }

// Permanent は打ち直しても変わらない失敗 (4xx など) の印。Do はすぐ返す。
func Permanent(err error) error {
	if err == nil {
		return nil
	}
	return permanentError{err}
}

func IsPermanent(err error) bool {
	var p permanentError
	return errors.As(err, &p)
}

// Do は fn が成功するか、Permanent を返すか、回数か ctx の期限が尽きるまで打ち直す。
func Do(ctx context.Context, p Policy, fn func(context.Context) error) error {
	for attempt := 0; ; attempt++ {
		err := fn(ctx)
		if err == nil {
			return nil
		}
		var perm permanentError
		if errors.As(err, &perm) {
			return perm.err
		}
		if attempt+1 >= p.Attempts {
			return err
		}
		t := time.NewTimer(Backoff(p, attempt, rand.Int64N))
		select {
		case <-ctx.Done():
			t.Stop()
			return errors.Join(err, ctx.Err())
		case <-t.C:
		}
	}
}

// Backoff は attempt 回目 (0 始まり) の待ち。上限 min(Max, Base*2^attempt) までの一様乱数 (full jitter)。
func Backoff(p Policy, attempt int, randN func(int64) int64) time.Duration {
	ceil := p.Max
	if attempt < 30 {
		if d := p.Base << attempt; d > 0 && d < ceil {
			ceil = d
		}
	}
	return time.Duration(randN(int64(ceil) + 1))
}
