package retry

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestBackoffIsCappedAndJittered(t *testing.T) {
	p := Policy{Attempts: 10, Base: 100 * time.Millisecond, Max: time.Second}
	maxRand := func(n int64) int64 { return n - 1 }
	for attempt, want := range []time.Duration{100 * time.Millisecond, 200 * time.Millisecond, 400 * time.Millisecond, 800 * time.Millisecond, time.Second, time.Second} {
		if got := Backoff(p, attempt, maxRand); got != want {
			t.Errorf("attempt %d: 上限 %v, 実際 %v", attempt, want, got)
		}
	}
	if got := Backoff(p, 3, func(int64) int64 { return 0 }); got != 0 {
		t.Errorf("full jitter の下限は 0: %v", got)
	}
	if got := Backoff(p, 100, maxRand); got != time.Second {
		t.Errorf("大きな attempt でも Max で止まる: %v", got)
	}
}

func TestDoRetriesUntilSuccess(t *testing.T) {
	n := 0
	err := Do(context.Background(), Policy{Attempts: 5, Base: time.Microsecond, Max: time.Microsecond}, func(context.Context) error {
		n++
		if n < 3 {
			return errors.New("一時的")
		}
		return nil
	})
	if err != nil || n != 3 {
		t.Fatalf("err=%v n=%d", err, n)
	}
}

func TestDoStopsOnPermanent(t *testing.T) {
	n := 0
	base := errors.New("422")
	err := Do(context.Background(), Default, func(context.Context) error { n++; return Permanent(base) })
	if !errors.Is(err, base) || n != 1 {
		t.Fatalf("err=%v n=%d", err, n)
	}
}

func TestDoGivesUpAfterAttempts(t *testing.T) {
	n := 0
	err := Do(context.Background(), Policy{Attempts: 3, Base: time.Microsecond, Max: time.Microsecond}, func(context.Context) error { n++; return errors.New("x") })
	if err == nil || n != 3 {
		t.Fatalf("err=%v n=%d", err, n)
	}
}

func TestDoHonoursDeadline(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	start := time.Now()
	err := Do(ctx, Policy{Attempts: 100, Base: time.Second, Max: time.Second}, func(context.Context) error { return errors.New("x") })
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) > 900*time.Millisecond {
		t.Fatalf("期限で止まらない: %v (%v)", err, time.Since(start))
	}
}
