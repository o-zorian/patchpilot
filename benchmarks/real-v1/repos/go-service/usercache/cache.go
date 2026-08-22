package usercache

type Cache struct {
	values map[string]User
}

func NewCache() *Cache                      { return &Cache{values: map[string]User{}} }
func (c *Cache) Get(id string) (User, bool) { value, ok := c.values[id]; return value, ok }
func (c *Cache) Put(user User)              { c.values[user.ID] = user }
