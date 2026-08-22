package pagetoken

import (
	"encoding/base64"
	"fmt"
	"strings"
)

func Decode(token string) (string, string, error) {
	raw, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		return "", "", err
	}
	parts := strings.Split(string(raw), ":")
	if len(parts) != 2 {
		return "", "", fmt.Errorf("invalid token")
	}
	return parts[0], parts[1], nil
}
