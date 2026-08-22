package usercache

import "testing"

func TestRenameDoesNotEvictOtherUsers(t *testing.T) {
	service := NewService([]User{{ID: "a", Name: "A"}, {ID: "b", Name: "B"}})
	_ = service.Get("a")
	_ = service.Get("b")
	service.Rename("a", "A2")
	if service.Get("a").Name != "A2" || service.Get("b").Name != "B" {
		t.Fatal("cache invalidation mismatch")
	}
}
