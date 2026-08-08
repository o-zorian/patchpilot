package benchmarkcases

import (
	"path/filepath"
	"testing"
)

func TestSafeJoinTraversal(t *testing.T) {
	root := t.TempDir()
	if _, err := SafeJoin(root, "../escape"); err == nil {
		t.Fatal("parent traversal should fail")
	}
	want := filepath.Join(root, "safe.txt")
	if got, err := SafeJoin(root, "safe.txt"); err != nil || got != want {
		t.Fatal("safe path did not resolve")
	}
}
