.PHONY: demo test seed

demo:
	./run.sh

test:
	uv run pytest -q

seed:
	MOCK_MODE=true uv run python -m app.seed
