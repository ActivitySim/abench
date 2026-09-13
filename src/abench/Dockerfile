ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential libhdf5-dev \
    && rm -rf /var/lib/apt/lists/*
COPY build_sources.py dependencies.json /opt/build/
RUN python /opt/build/build_sources.py /opt/build/dependencies.json
ENV PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 NUMBA_NUM_THREADS=1 PYTHONHASHSEED=0 DASK_SCHEDULER=synchronous
WORKDIR /model
ENTRYPOINT ["python", "/benchmark/worker.py"]
