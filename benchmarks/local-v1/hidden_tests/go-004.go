package benchmarkcases

import (
	"reflect"
	"testing"
)

func TestSortDoesNotMutateInput(t *testing.T) {
	input := []string{"b", "a"}
	output := SortNames(input)
	if !reflect.DeepEqual(output, []string{"a", "b"}) || !reflect.DeepEqual(input, []string{"b", "a"}) {
		t.Fatal("sort result or input mutation is incorrect")
	}
}
