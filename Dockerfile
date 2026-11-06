FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY ledger ./ledger
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md
RUN pip install --no-cache-dir '.[server]' && useradd --uid 10001 --create-home ledger
USER ledger
EXPOSE 3000
CMD ["gunicorn", "--bind", "0.0.0.0:3000", "--workers", "2", "--timeout", "30", "ledger.cli:make_app()"]
