install:
	pip install -r requirements.txt

run:
	streamlit run app.py --server.port 8501

api:
	uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000

web:
	cd frontend && npm run dev

test:
	pytest

smoke:
	python3 scripts/smoke_test.py
