package retry

import (
	"errors"
	"testing"
)

func TestJoinedTemporaryErrorRetries(t *testing.T) {
	if !ShouldRetry(errors.Join(errors.New("context"), ErrTemporary)) {
		t.Fatal("expected retry")
	}
}
