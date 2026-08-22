package pagetoken

import "encoding/base64"

func Encode(tenant, cursor string) string {
	return base64.RawURLEncoding.EncodeToString([]byte(tenant + ":" + cursor))
}
