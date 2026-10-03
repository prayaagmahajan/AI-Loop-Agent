PYTHON ?= python3
export PYTHONPATH := src

.PHONY: setup test validate baseline eval report demo
setup:
    $(PYTHON) -m eval_loop fetch-data --require-pin
test: setup
    $(PYTHON) -m unittest discover -s tests -v
validate: setup
    $(PYTHON) -m eval_loop validate
baseline: setup
    $(PYTHON) -m eval_loop run --config configs/nemotron-v3.json --id local-baseline
eval: setup
    $(PYTHON) -m eval_loop run --config configs/nemotron-v3.json --id local-candidate
    $(PYTHON) -m eval_loop compare --baseline runs/baseline-promoted --candidate runs/local-candidate
report:
    $(PYTHON) -m eval_loop report
demo:
    $(PYTHON) -m eval_loop compare --baseline runs/baseline --candidate runs/regression; code=$$?; test $$code -eq 1
    $(PYTHON) -m eval_loop report