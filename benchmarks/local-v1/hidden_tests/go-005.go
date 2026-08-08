package benchmarkcases

import "testing"

func TestDecodeEnabledJSON(t *testing.T) {
	value, err := DecodeEnabled([]byte(`{"enabled":false}`))
	if err != nil || value {
		t.Fatal("JSON false was not decoded")
	}
	if _, err = DecodeEnabled([]byte(`not-json`)); err == nil {
		t.Fatal("invalid JSON should return an error")
	}
}
