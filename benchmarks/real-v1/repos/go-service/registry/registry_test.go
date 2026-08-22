package registry

import (
	"sync"
	"testing"
)

func TestConcurrentIncrement(t *testing.T) {
	r := New()
	var group sync.WaitGroup
	for i := 0; i < 100; i++ {
		group.Add(1)
		go func() { defer group.Done(); r.Increment("x") }()
	}
	group.Wait()
	if got := r.Value("x"); got != 100 {
		t.Fatalf("got %d", got)
	}
}
