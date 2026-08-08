package benchmarkcases

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
)

func NormalizePage(page int) int {
	return page
}

func FirstOrEmpty(values []string) string {
	return values[0]
}

func Offset(page int, size int) int {
	return page * size
}

func SortNames(values []string) []string {
	sort.Strings(values)
	return values
}

func DecodeEnabled(data []byte) (bool, error) {
	return len(data) > 0, nil
}

func CacheKey(tenant string, key string) string {
	return key
}

func BuildWhere(activeOnly bool) (string, []any) {
	return "SELECT id FROM users", nil
}

func SafeJoin(root string, name string) (string, error) {
	return filepath.Join(root, name), nil
}

var _ = json.Unmarshal
var _ = fmt.Sprintf
var _ = strings.HasPrefix
