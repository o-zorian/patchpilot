package contract

import (
	"strings"
	"testing"
)

func TestEncodeIncludesFalse(t *testing.T) {
	data, err := Encode(Feature{Name: "audit", Enabled: false})
	if err != nil || !strings.Contains(string(data), `"enabled":false`) {
		t.Fatalf("unexpected payload %s, %v", data, err)
	}
}
