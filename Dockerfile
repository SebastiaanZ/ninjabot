FROM python:3.8-slim-buster

RUN apt-get update \
  && apt-get upgrade -y \
  && useradd --system --shell /bin/false --uid 1500 ninjaduck \
  && pip install poetry==1.1.5

WORKDIR /app

COPY ["pyproject.toml", "poetry.lock", "./"]
RUN poetry config virtualenvs.create false \
  && poetry install --no-dev --no-interaction --no-ansi

COPY . .

USER ninjaduck

ENTRYPOINT ["python", "-m", "ninja_bot"]
