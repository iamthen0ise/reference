.PHONY: init run build check test check-vault check-dist check-browser init-browser sql-up sql-run sql-block sql-bg sql-psql sql-down sql-test

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

# Прогон SQL на PostgreSQL 17 в Docker (схема и данные — со страницы «Схема библиотеки»).
SEED ?= small

sql-up:
	bash .site/scripts/sql-pg.sh up $(SEED)

sql-run:
	bash .site/scripts/sql-pg.sh run "$(FILE)"

sql-block:
	bash .site/scripts/sql-pg.sh block "$(PAGE)" "$(TITLE)"

sql-bg:
	bash .site/scripts/sql-pg.sh bg "$(PAGE)" "$(TITLE)"

sql-psql:
	docker exec -it reference-pg psql -U postgres -d library

sql-down:
	bash .site/scripts/sql-pg.sh down

sql-test:
	bash .site/scripts/sql-pg.test.sh
