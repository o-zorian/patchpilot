package idempotency

func Execute(state *State, key string, operation func() (int, error)) (int, error) {
	if value, done := state.Begin(key); done {
		return value, nil
	}
	value, err := operation()
	if err != nil {
		return 0, err
	}
	state.Complete(key, value)
	return value, nil
}
