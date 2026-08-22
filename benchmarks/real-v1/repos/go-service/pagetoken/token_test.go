package pagetoken

import "testing"

func TestRoundTripTenantContainingColon(t *testing.T) {
	tenant, cursor, err := Decode(Encode("region:tenant", "42"))
	if err != nil || tenant != "region:tenant" || cursor != "42" {
		t.Fatalf("got %q %q %v", tenant, cursor, err)
	}
}
