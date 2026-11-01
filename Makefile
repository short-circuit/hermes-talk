# Local-only helper targets for the Hermes Talk relay.

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python)

.PHONY: prune-logs

# Prune $DATA_DIR/logs and $DATA_DIR/audit.log by age and size.
# Runs offline and never creates anything inside the repository.
prune-logs:
	PYTHONPATH=$(CURDIR) $(PYTHON) -m relay.logging.prune $(PRUNE_ARGS)
