PYTHON ?= .venv/bin/python
PYTHON_BOOTSTRAP ?= python3.13
COLLECT_COMMAND ?= $(PYTHON) -m mdv.cli --config config/config.yaml collect
PACKAGE_DIR ?= .tmp/package-dist
PACKAGE_SMOKE_VENV ?= .tmp/package-smoke
BACKUP_DIR ?= .local/backups
BACKUP_FILE ?= $(BACKUP_DIR)/asset-master-data-runtime.tar.gz
DB_PATH ?=

-include Makefile.local

.PHONY: install install-prod test check run package package-smoke backup restore restore-check collect collect-prod serve install-systemd deploy-prod prod-status prod-logs clean-data

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

backup:
	install -d -m 0700 $(BACKUP_DIR)
	DB_PATH_VALUE="$(DB_PATH)"; if [ -z "$$DB_PATH_VALUE" ]; then DB_PATH_VALUE="$$($(PYTHON) -m mdv.cli --config config/config.yaml config-value database.path)"; fi; case "$$DB_PATH_VALUE" in /*) ;; *) DB_PATH_VALUE="$(CURDIR)/$$DB_PATH_VALUE" ;; esac; $(PYTHON) scripts/runtime_backup.py create --output $(BACKUP_FILE) --sqlite "$$DB_PATH_VALUE" --path "$(CURDIR)/config/config.yaml"

restore:
	@if command -v systemctl >/dev/null 2>&1 && (systemctl is-active --quiet asset-master-data.service || systemctl is-active --quiet asset-master-refresh.service || systemctl is-active --quiet asset-master-refresh.timer); then echo "Refusing restore while asset-master-data, collection service, or collection timer is active" >&2; exit 1; fi
	$(PYTHON) scripts/runtime_backup.py restore $(BACKUP_FILE) --target-root / --replace

restore-check:
	$(PYTHON) scripts/runtime_backup.py verify $(BACKUP_FILE)

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
	@echo "Refusing to delete runtime data automatically. Remove .data only when intended."
