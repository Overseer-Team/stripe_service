FROM python:3.12.7

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    \
    PDM_CHECK_UPDATE=false

RUN apt update \
    && apt install -y \
    git

RUN pip install -U pdm

WORKDIR /main
COPY pdm.lock pyproject.toml ./

RUN pdm install --check --prod --no-editable

COPY . .
ENTRYPOINT ["pdm", "run", "gunicorn", "app:app", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8888"]
