PYTHON ?= .venv/bin/python
PYTHON_BOOTSTRAP ?= python3.13
COLLECT_COMMAND ?= $(PYTHON) -m mdv.cli --config config/config.yaml collect

-include Makefile.local

.PHONY: install test collect serve install-systemd deploy-prod clean-data

install:
	$(PYTHON_BOOTSTRAP) -m venv .venv
	.venv/bin/pip install -e '.[dev]'

test:
	$(PYTHON) -m pytest -q

collect:
	$(COLLECT_COMMAND)

serve:
	$(PYTHON) -m mdv.cli --config config/config.yaml serve

install-systemd:
	bash deploy/systemd/install_systemd.sh

deploy-prod:
	ssh tradier 'cd /home/ubuntu/asset-master-data && bash deploy/systemd/deploy.sh'

clean-data:
	@echo "Refusing to delete runtime data automatically. Remove .data only when intended."
