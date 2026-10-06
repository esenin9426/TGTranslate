FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONPATH=/srv PYTHONUNBUFFERED=1

# Dependencies and the spaCy model go first: they only change with pyproject.toml, so editing the code
# does not reinstall them. The stub package lets `pip install .` resolve the dependencies without the sources.
COPY pyproject.toml ./
RUN mkdir app && touch app/__init__.py \
    && pip install --no-cache-dir . \
    && pip uninstall -y tgtranslate \
    && rm -r app \
    && python -m spacy download en_core_web_sm

# Run as an unprivileged user; /data/uploads is created owned by it so a fresh named volume inherits that.
RUN useradd --create-home app && mkdir -p /data/uploads && chown app /data/uploads

COPY alembic.ini ./
COPY app app
COPY migrations migrations
USER app

# The command is set per service in docker-compose.yml
