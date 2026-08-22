package ledger

func Transfer(store *Store, source, target string, amount int) error {
	if err := store.Debit(source, amount); err != nil {
		return err
	}
	return store.Credit(target, amount)
}
