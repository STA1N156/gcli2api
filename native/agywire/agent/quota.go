package agent

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"time"
)

const quotaRPCTimeout = 45 * time.Second

func (d *Daemon) fetchQuotaSummary(ctx context.Context) ([]byte, error) {
	b, code, err := d.controlCall(ctx, "POST", Endpoint+"/v1internal:retrieveUserQuotaSummary",
		[]byte(`{"project":"`+Project+`"}`), true)
	if err != nil {
		return nil, err
	}
	if code != 200 {
		return nil, fmt.Errorf("retrieveUserQuotaSummary HTTP %d", code)
	}
	cs := &d.ctrl
	cs.mu.Lock()
	cs.quotaRaw, cs.quotaAt = b, time.Now()
	cs.mu.Unlock()
	return b, nil
}

func (d *Daemon) wireQuota(ctx context.Context) (any, error) {
	if _, err := d.Catalog(ctx); err != nil {
		return nil, err
	}
	cs := &d.ctrl
	cs.quotaFetch.Lock()
	defer cs.quotaFetch.Unlock()
	cs.mu.Lock()
	lca, summary, at := cs.lcaRaw, cs.quotaRaw, cs.quotaAt
	cs.mu.Unlock()
	if summary == nil || time.Since(at) >= quotaCacheFresh {
		b, err := d.fetchQuotaSummary(ctx)
		switch {
		case err == nil:
			summary = b
		case summary == nil:
			return nil, err
		default:
			log.Printf("额度汇总重查失败,沿用 %s 前的缓存: %v", time.Since(at).Round(time.Second), err)
		}
	}
	if lca == nil {
		return nil, errors.New("loadCodeAssist 还没有成功过,暂时拿不到档位")
	}
	return map[string]json.RawMessage{"loadCodeAssist": lca, "summary": summary}, nil
}
