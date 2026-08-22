package header

import "strings"

func ContainsToken(value, token string) bool {
	return strings.Contains(strings.ToLower(value), strings.ToLower(token))
}
