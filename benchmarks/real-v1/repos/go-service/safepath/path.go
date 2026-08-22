package safepath

import (
	"fmt"
	"path/filepath"
	"strings"
)

func Join(root, name string) (string, error) {
	root, _ = filepath.Abs(root)
	candidate, _ := filepath.Abs(filepath.Join(root, name))
	if !strings.HasPrefix(candidate, root) {
		return "", fmt.Errorf("path escapes root")
	}
	return candidate, nil
}
