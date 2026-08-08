package benchmarkcases

import "testing"

func TestBuildWhereActive(t *testing.T) {
	query, args := BuildWhere(true)
	if query != "SELECT id FROM users WHERE active = ?" || len(args) != 1 || args[0] != true {
		t.Fatal("active condition is missing")
	}
}
