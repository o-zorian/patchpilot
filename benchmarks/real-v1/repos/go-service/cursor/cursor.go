package cursor

import (
	"fmt"
	"strconv"
	"strings"
)

type Cursor struct {
	Offset int
	Limit  int
}

func Parse(value string) (Cursor, error) {
	parts := strings.Split(value, ":")
	if len(parts) != 2 {
		return Cursor{}, fmt.Errorf("invalid cursor")
	}
	offset, err := strconv.Atoi(parts[0])
	if err != nil {
		return Cursor{}, fmt.Errorf("invalid offset")
	}
	limit, err := strconv.Atoi(parts[1])
	if err != nil {
		return Cursor{}, fmt.Errorf("invalid limit")
	}
	return Cursor{Offset: offset, Limit: limit}, nil
}
