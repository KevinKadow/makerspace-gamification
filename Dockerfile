FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY ledger ./ledger
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md
RUN pip install --no-cache-dir '.[server]' && useradd --uid 10001 --create-home ledger && mkdir -p /var/lib/ledger-avatars && chown ledger:ledger /var/lib/ledger-avatars
USER ledger
EXPOSE 3000
CMD ["python", "-m", "ledger.server"]
