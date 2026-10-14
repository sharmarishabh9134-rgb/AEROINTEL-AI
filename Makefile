.PHONY: demo test train map simulate

demo:
	docker compose up --build

test:
	python -m pytest -q

train:
	python -m backend.scripts.train_fd001

map:
	python -m backend.scripts.show_snag_mappings

simulate:
	python -m backend.scripts.simulate_maintenance
