.PHONY: init run build check test check-vault check-dist check-browser init-browser

PREVIEW_PORT ?= 4329
PLAYWRIGHT_INSTALL_FLAGS ?=

init:
	cd .site && npm ci
	$(MAKE) init-browser

init-browser:
	cd .site && npx playwright install $(PLAYWRIGHT_INSTALL_FLAGS) chromium

run:
	cd .site && npm run dev

build:
	cd .site && npm run build

check: check-vault build check-dist

test:
	node --test '.site/scripts/*.test.mjs' '.site/src/plugins/*.test.mjs'

check-vault:
	node .site/scripts/check-vault.mjs .

check-dist:
	node .site/scripts/check-dist.mjs .site/dist --base /reference --vault .

# Нужна готовая сборка (make build). Превью поднимается и гасится скриптом.
check-browser:
	cd .site && sh scripts/check-browser.sh $(PREVIEW_PORT)
