package usercache

import "testing"

func TestRenameInvalidatesCache(t *testing.T) {
	service := NewService([]User{{ID: "u", Name: "before"}})
	_ = service.Get("u")
	service.Rename("u", "after")
	if got := service.Get("u").Name; got != "after" {
		t.Fatalf("got %q", got)
	}
}
