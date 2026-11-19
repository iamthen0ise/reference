.PHONY: init run build check test check-vault check-dist

init:
	cd .site && npm ci && npx playwright install chromium

run:
	cd .site && npm run dev

build:
	cd .site && npm run build

check: check-vault build check-dist

test:
	node --test .site/scripts/ .site/src/plugins/

check-vault:
	node .site/scripts/check-vault.mjs .

check-dist:
	node .site/scripts/check-dist.mjs .site/dist --base /reference --mermaid 32 --asides 30
