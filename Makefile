.PHONY: init run build check test check-vault check-dist check-browser init-browser

PREVIEW_PORT ?= 4321

init:
	cd .site && npm ci
	$(MAKE) init-browser

init-browser:
	cd .site && npx playwright install chromium

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

# Нужна готовая сборка (make build). Превью поднимается и гасится скриптом.
check-browser:
	cd .site && sh scripts/check-browser.sh $(PREVIEW_PORT)
