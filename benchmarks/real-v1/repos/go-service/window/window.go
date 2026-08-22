package window

func Normalize(page, size, maximum int) (int, int) {
	if page < 0 {
		page = 1
	}
	if size <= 0 {
		size = 20
	}
	if size > maximum {
		size = maximum
	}
	return page, size
}
