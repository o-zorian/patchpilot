package wrapped

import "fmt"

func Lookup(id string) error {
	return fmt.Errorf("lookup %s: %v", id, ErrNotFound)
}
