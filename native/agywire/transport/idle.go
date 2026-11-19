package transport

import (
	"context"
	"errors"
	"io"
	"net/http"
	"sync/atomic"
	"time"
)

const StreamIdleTimeout = 600 * time.Second

var ErrStreamIdle = errors.New("上游流式空闲超时:长时间没有任何数据")

func DoStream(ctx context.Context, client *http.Client, s Spec, idle time.Duration) (*http.Response, error) {
	ctx, cancel := context.WithCancel(ctx)
	fired := &atomic.Bool{}
	timer := time.AfterFunc(idle, func() {
		fired.Store(true)
		cancel()
	})
	req, err := Build(ctx, s)
	if err != nil {
		timer.Stop()
		cancel()
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		timer.Stop()
		cancel()
		if fired.Load() {
			return nil, ErrStreamIdle
		}
		return nil, err
	}
	resp.Body = &idleBody{rc: resp.Body, timer: timer, idle: idle, cancel: cancel, fired: fired}
	return resp, nil
}

type idleBody struct {
	rc     io.ReadCloser
	timer  *time.Timer
	idle   time.Duration
	cancel context.CancelFunc
	fired  *atomic.Bool
}

func (b *idleBody) Read(p []byte) (int, error) {
	n, err := b.rc.Read(p)
	if n > 0 && !b.fired.Load() {
		b.timer.Reset(b.idle)
	}
	if err != nil && !errors.Is(err, io.EOF) && b.fired.Load() {
		return n, ErrStreamIdle
	}
	return n, err
}

func (b *idleBody) Close() error {
	b.timer.Stop()
	err := b.rc.Close()
	b.cancel()
	return err
}
