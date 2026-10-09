.PHONY: run seed test

run:
	./run.sh

seed:
	python -m scripts.seed

test:
	python -m pytest