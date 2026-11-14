.PHONY: install backend frontend test

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m app.tests.test_transform_chain && .venv/bin/python -m app.tests.test_api_chain
	cd frontend && npm run typecheck
