# The cluster's Ray image plus what the ray_jobs entrypoints import. The base is the same pinned image as
# k8s apps/ray/app/raycluster.yaml (Ray's head and workers must run one Ray version); keep the two in step.
# Only dependencies are baked in: the job code itself still arrives as the zip core-api stages (it wins on sys.path),
# so a job-code change needs no image rebuild. Dependency source of truth: pyproject.toml.
FROM rayproject/ray:2.60.0.8f2cbc-py311-cu128@sha256:76e91000ac4eab6b53c5d796bc62ad61a519ba10c6a3bc2411eb651d8f5d067a

COPY pyproject.toml /tmp/ray-jobs/pyproject.toml
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/ray-jobs/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/ray-jobs/requirements.txt \
    && pip install --no-cache-dir -r /tmp/ray-jobs/requirements.txt \
    && python -c "import boto3, pyarrow, PIL; print('boto3', boto3.__version__, 'pyarrow', pyarrow.__version__, 'Pillow', PIL.__version__)" \
    && rm -rf /tmp/ray-jobs
