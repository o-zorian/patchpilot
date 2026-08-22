package idempotency

type State struct {
	done map[string]int
}

func NewState() *State { return &State{done: map[string]int{}} }
func (s *State) Begin(key string) (int, bool) {
	value, ok := s.done[key]
	if !ok {
		s.done[key] = 0
	}
	return value, ok
}
func (s *State) Complete(key string, value int) { s.done[key] = value }
