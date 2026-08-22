package registry

import (
	"sync"
	"testing"
)

func TestConcurrentReadersAndWriters(t *testing.T) {
	r := New()
	var group sync.WaitGroup
	for i := 0; i < 50; i++ {
		group.Add(2)
		go func() { defer group.Done(); r.Increment("k") }()
		go func() { defer group.Done(); _ = r.Value("k") }()
	}
	group.Wait()
	if r.Value("k") != 50 {
		t.Fatalf("got %d", r.Value("k"))
	}
}
