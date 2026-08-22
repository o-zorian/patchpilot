package contract

import "encoding/json"

func Encode(feature Feature) ([]byte, error) {
	payload := struct {
		Name    string `json:"name"`
		Enabled bool   `json:"enabled,omitempty"`
	}{Name: feature.Name, Enabled: feature.Enabled}
	return json.Marshal(payload)
}
