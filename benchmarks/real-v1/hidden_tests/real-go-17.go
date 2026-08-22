package safepath

import (
	"path/filepath"
	"testing"
)

func TestNestedChildAccepted(t *testing.T) {
	root := t.TempDir()
	got, err := Join(root, "a/b.txt")
	if err != nil || got != filepath.Join(root, "a", "b.txt") {
		t.Fatalf("got %q %v", got, err)
	}
}
