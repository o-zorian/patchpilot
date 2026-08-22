package retry

import "errors"

var ErrTemporary = errors.New("temporary")

func ShouldRetry(err error) bool {
	return err == ErrTemporary
}
