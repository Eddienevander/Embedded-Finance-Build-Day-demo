.PHONY: demo test seed record preflight

demo:
	./run.sh

test:
	uv run pytest -q

seed:
	MOCK_MODE=true uv run python -m app.seed

# Re-record the replay fixtures (costs API calls; commit the result).
record:
	MOCK_MODE=true uv run python -m app.record

preflight:
	uv run python -m app.preflight
