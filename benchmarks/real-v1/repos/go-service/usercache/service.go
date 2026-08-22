package usercache

type User struct {
	ID   string
	Name string
}

type Service struct {
	store map[string]User
	cache *Cache
}

func NewService(users []User) *Service {
	store := map[string]User{}
	for _, user := range users {
		store[user.ID] = user
	}
	return &Service{store: store, cache: NewCache()}
}

func (s *Service) Get(id string) User {
	if value, ok := s.cache.Get(id); ok {
		return value
	}
	value := s.store[id]
	s.cache.Put(value)
	return value
}

func (s *Service) Rename(id, name string) {
	value := s.store[id]
	value.Name = name
	s.store[id] = value
}
