package ledger

import "fmt"

type Store struct {
	Balances   map[string]int
	FailCredit string
}

func (s *Store) Debit(id string, amount int) error {
	if amount <= 0 || s.Balances[id] < amount {
		return fmt.Errorf("invalid debit")
	}
	s.Balances[id] -= amount
	return nil
}

func (s *Store) Credit(id string, amount int) error {
	if id == s.FailCredit {
		return fmt.Errorf("credit failed")
	}
	s.Balances[id] += amount
	return nil
}
