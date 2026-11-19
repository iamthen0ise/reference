.PHONY: test check-vault check-dist

test:
	node --test .site/scripts/

check-vault:
	node .site/scripts/check-vault.mjs .

check-dist:
	node .site/scripts/check-dist.mjs .site/dist --base /reference --mermaid 32 --asides 30
