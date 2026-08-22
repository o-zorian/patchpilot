package safepath

import (
	"path/filepath"
	"testing"
)

func TestSiblingPrefixRejected(t *testing.T) {
	root := filepath.Join(t.TempDir(), "app")
	if _, err := Join(root, "../app-old/secret"); err == nil {
		t.Fatal("expected rejection")
	}
}
