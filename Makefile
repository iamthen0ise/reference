.PHONY: test check-vault

test:
	node --test .site/scripts/

check-vault:
	node .site/scripts/check-vault.mjs .
