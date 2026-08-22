package retry

import (
	"fmt"
	"testing"
)

func TestWrappedTemporaryErrorRetries(t *testing.T) {
	if !ShouldRetry(fmt.Errorf("request: %w", ErrTemporary)) {
		t.Fatal("expected retry")
	}
}
