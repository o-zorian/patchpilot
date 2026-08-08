package benchmarkcases

import "testing"

func TestCacheKeyTenantScope(t *testing.T) {
	if CacheKey("north", "profile") != "north:profile" || CacheKey("north", "profile") == CacheKey("south", "profile") {
		t.Fatal("cache key is not tenant scoped")
	}
}
