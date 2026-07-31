PYTHON ?= .venv/bin/python
PYTHON_BOOTSTRAP ?= python3.13
COLLECT_COMMAND ?= $(PYTHON) -m mdv.cli --config config/config.yaml collect
PACKAGE_DIR ?= .tmp/package-dist
PACKAGE_SMOKE_VENV ?= .tmp/package-smoke
BACKUP_DIR ?= .local/backups
BACKUP_FILE ?= $(BACKUP_DIR)/asset-master-data-runtime.tar.gz
CONFIG_PATH ?= config/config.yaml
DB_PATH ?=
LEGACY_DB_PATH ?= .data/mdv.sqlite3
RUNTIME_VERSION ?= $(shell $(PYTHON) -c 'import mdv; print(mdv.__version__)')
RUNTIME_REVISION ?= $(shell git rev-parse --verify HEAD)
RUNTIME_RELEASE ?= v$(RUNTIME_VERSION)-$(shell git rev-parse --short=12 HEAD)

-include Makefile.local

.PHONY: install install-prod test check run package package-smoke backup restore restore-check migrate-state collect collect-prod serve install-systemd deploy-prod prod-status prod-logs clean-data

install:
	$(PYTHON_BOOTSTRAP) -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements-dev.lock
	cd / && "$(CURDIR)/.venv/bin/pip" install --no-deps -e "$(CURDIR)"

install-prod:
	$(PYTHON_BOOTSTRAP) -m venv .venv
	.venv/bin/pip install --require-hashes -r requirements.lock
	cd / && "$(CURDIR)/.venv/bin/pip" install --no-deps "$(CURDIR)"

test:
	$(PYTHON) -m pytest -q

check: test package-smoke
	git diff --check

run: serve

package:
	mkdir -p $(PACKAGE_DIR)
	$(PYTHON) -m build --wheel --no-isolation --outdir $(PACKAGE_DIR)

package-smoke: package
	$(PYTHON_BOOTSTRAP) -m venv --clear $(PACKAGE_SMOKE_VENV)
	$(PACKAGE_SMOKE_VENV)/bin/pip install --require-hashes -r requirements.lock
	cd / && "$(CURDIR)/$(PACKAGE_SMOKE_VENV)/bin/pip" install --no-deps "$$(ls -t "$(CURDIR)/$(PACKAGE_DIR)"/*.whl | head -1)"
	$(PACKAGE_SMOKE_VENV)/bin/python -c 'import re; from importlib.metadata import version; import mdv; installed = version("asset-master-data"); assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", installed); assert installed == mdv.__version__'
	cd / && "$(CURDIR)/$(PACKAGE_SMOKE_VENV)/bin/python" -m mdv.runtime_backup --help >/dev/null

backup:
	install -d -m 0700 $(BACKUP_DIR)
	DB_PATH_VALUE="$(DB_PATH)"; if [ -z "$$DB_PATH_VALUE" ]; then DB_PATH_VALUE="$$($(PYTHON) -m mdv.cli --config "$(CONFIG_PATH)" config-value database.path)"; fi; case "$$DB_PATH_VALUE" in /*) ;; *) DB_PATH_VALUE="$(CURDIR)/$$DB_PATH_VALUE" ;; esac; $(PYTHON) -m mdv.runtime_backup create --output $(BACKUP_FILE) --state-dir "$$(dirname "$$DB_PATH_VALUE")" --database-name "$$(basename "$$DB_PATH_VALUE")" --release "$(RUNTIME_RELEASE)" --revision "$(RUNTIME_REVISION)" --version "$(RUNTIME_VERSION)" --configuration "$(CONFIG_PATH)"

restore:
	@if command -v systemctl >/dev/null 2>&1 && (systemctl is-active --quiet asset-master-data.service || systemctl is-active --quiet asset-master-refresh.service || systemctl is-active --quiet asset-master-refresh.timer); then echo "Refusing restore while asset-master-data, collection service, or collection timer is active" >&2; exit 1; fi
	DB_PATH_VALUE="$(DB_PATH)"; if [ -z "$$DB_PATH_VALUE" ]; then DB_PATH_VALUE="$$($(PYTHON) -m mdv.cli --config "$(CONFIG_PATH)" config-value database.path)"; fi; case "$$DB_PATH_VALUE" in /*) ;; *) DB_PATH_VALUE="$(CURDIR)/$$DB_PATH_VALUE" ;; esac; $(PYTHON) -m mdv.runtime_backup restore $(BACKUP_FILE) --target "$$(dirname "$$DB_PATH_VALUE")" --replace --release "$(RUNTIME_RELEASE)" --revision "$(RUNTIME_REVISION)" --version "$(RUNTIME_VERSION)" --configuration "$(CONFIG_PATH)"

restore-check:
	$(PYTHON) -m mdv.runtime_backup verify $(BACKUP_FILE)

migrate-state:
	@if command -v systemctl >/dev/null 2>&1 && (systemctl is-active --quiet asset-master-data.service || systemctl is-active --quiet asset-master-refresh.service || systemctl is-active --quiet asset-master-refresh.timer); then echo "Refusing migration while asset-master-data, collection service, or collection timer is active" >&2; exit 1; fi
	@if command -v lsof >/dev/null 2>&1 && lsof "$(LEGACY_DB_PATH)" >/dev/null; then echo "Refusing migration while the legacy database is open" >&2; exit 1; fi
	@test -f "$(LEGACY_DB_PATH)" || { echo "Legacy database does not exist: $(LEGACY_DB_PATH)" >&2; exit 1; }
	DB_PATH_VALUE="$$($(PYTHON) -m mdv.cli --config "$(CONFIG_PATH)" config-value database.path)"; case "$$DB_PATH_VALUE" in /*) ;; *) DB_PATH_VALUE="$(CURDIR)/$$DB_PATH_VALUE" ;; esac; LEGACY_DB_VALUE="$(LEGACY_DB_PATH)"; case "$$LEGACY_DB_VALUE" in /*) ;; *) LEGACY_DB_VALUE="$(CURDIR)/$$LEGACY_DB_VALUE" ;; esac; test ! -e "$$(dirname "$$DB_PATH_VALUE")" || { echo "Target state directory already exists: $$(dirname "$$DB_PATH_VALUE")" >&2; exit 1; }; install -d -m 0700 $(BACKUP_DIR); $(PYTHON) -m mdv.runtime_backup create --output $(BACKUP_FILE) --state-dir "$$(dirname "$$LEGACY_DB_VALUE")" --database-name "$$(basename "$$LEGACY_DB_VALUE")" --release "$(RUNTIME_RELEASE)" --revision "$(RUNTIME_REVISION)" --version "$(RUNTIME_VERSION)" --configuration "$(CONFIG_PATH)"; $(PYTHON) -m mdv.runtime_backup restore $(BACKUP_FILE) --target "$$(dirname "$$DB_PATH_VALUE")" --release "$(RUNTIME_RELEASE)" --revision "$(RUNTIME_REVISION)" --version "$(RUNTIME_VERSION)" --configuration "$(CONFIG_PATH)"

collect:
	$(COLLECT_COMMAND)

serve:
	$(PYTHON) -m mdv.cli --config config/config.yaml serve

install-systemd:
	bash deploy/systemd/install_systemd.sh

deploy-prod:
	ssh tradier 'cd /home/ubuntu/asset-master-data && bash deploy/systemd/deploy.sh'

prod-status:
	ssh tradier 'systemctl is-active asset-master-data.service asset-master-refresh.timer && systemctl --no-pager list-timers asset-master-refresh.timer'

prod-logs:
	ssh tradier 'journalctl -u asset-master-data -u asset-master-refresh --since "30 minutes ago" --no-pager | tail -200'

collect-prod:
	ssh tradier 'cd /home/ubuntu/asset-master-data && .local/current/venv/bin/python -m mdv.cli --config .local/current/config/config.yaml collect'

clean-data:
	@echo "Refusing to delete runtime data automatically. Remove .local/state only when intended."
