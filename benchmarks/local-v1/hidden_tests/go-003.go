package benchmarkcases

import "testing"

func TestOffsetFirstPage(t *testing.T) {
	if Offset(1, 10) != 0 || Offset(2, 10) != 10 {
		t.Fatal("pagination offset is incorrect")
	}
}
