package main

import (
	"net/http"
	"testing"
	"time"
)

func TestCachedTransportSkipsCreationLock(t *testing.T) {
	b := &bridge{}
	cached, err := b.transport("account", "direct")
	if err != nil {
		t.Fatal(err)
	}
	defer cached.CloseIdleConnections()
	b.mu.Lock()
	defer b.mu.Unlock()
	result := make(chan *http.Transport, 1)
	go func() {
		transport, _ := b.transport("account", "direct")
		result <- transport
	}()
	select {
	case actual := <-result:
		if actual != cached {
			t.Fatal("cached transport was replaced")
		}
	case <-time.After(time.Second):
		t.Fatal("cached lookup waited for the creation lock")
	}
}
