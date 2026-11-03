# The CPU Ray image plus what the ray_jobs entrypoints import: the image of the cluster's CPU worker group, which runs
# the ingest jobs. The base is the same Ray version as k8s apps/ray/app/raycluster.yaml (head and workers must run one
# Ray version); keep the two in step.
# Only dependencies are baked in: the job code itself still arrives as the zip core-api stages (it wins on sys.path),
# so a job-code change needs no image rebuild. Dependency source of truth: pyproject.toml, except the two lines below it:
# the training driver runs on this group too and starts Ray Train, whose TorchTrainer imports torch (CPU wheel is enough).
# BASE is the amd64 image of the cluster; docker-compose.yml overrides it with the arm64 build for Apple Silicon.
ARG BASE=rayproject/ray:2.60.0.8f2cbc-py311-cpu@sha256:64f1f57a8c547f7582fe985437f083bfb72f63d6c54987ecad98f9505a380ceb
FROM ${BASE}

COPY --chown=ray:users pyproject.toml /tmp/ray-jobs/pyproject.toml
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/ray-jobs/pyproject.toml', 'rb'))['project']['dependencies']))" \
        > /tmp/ray-jobs/requirements.txt \
    && pip install --no-cache-dir -r /tmp/ray-jobs/requirements.txt \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir "ray[train]==$(python -c 'import ray; print(ray.__version__)')" \
    && python -c "import ray.train.torch, boto3, pyarrow, PIL; print('boto3', boto3.__version__, 'pyarrow', pyarrow.__version__, 'Pillow', PIL.__version__)" \
    && rm -rf /tmp/ray-jobs
