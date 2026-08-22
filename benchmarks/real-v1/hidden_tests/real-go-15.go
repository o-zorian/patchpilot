package contract

import (
	"strings"
	"testing"
)

func TestEncodeIncludesTrueAndFalseContractField(t *testing.T) {
	for _, enabled := range []bool{false, true} {
		data, err := Encode(Feature{Name: "x", Enabled: enabled})
		if err != nil || !strings.Contains(string(data), `"enabled":`) {
			t.Fatalf("payload=%s err=%v", data, err)
		}
	}
}
