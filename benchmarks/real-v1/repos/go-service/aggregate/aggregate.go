package aggregate

func Counts(values []string) []Count {
	counts := map[string]int{}
	for _, value := range values {
		counts[value]++
	}
	result := make([]Count, 0, len(counts))
	for key, value := range counts {
		result = append(result, Count{Key: key, Value: value})
	}
	return result
}
