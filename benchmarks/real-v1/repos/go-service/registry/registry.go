package registry

type Registry struct{ values map[string]int }

func New() *Registry                     { return &Registry{values: map[string]int{}} }
func (r *Registry) Increment(key string) { r.values[key]++ }
func (r *Registry) Value(key string) int { return r.values[key] }
