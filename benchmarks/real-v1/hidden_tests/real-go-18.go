package pagetoken

import "testing"

func TestRoundTripReservedCharacters(t *testing.T) {
	tenant, cursor, err := Decode(Encode("a:b&c", "x:y=z"))
	if err != nil || tenant != "a:b&c" || cursor != "x:y=z" {
		t.Fatalf("got %q %q %v", tenant, cursor, err)
	}
}
