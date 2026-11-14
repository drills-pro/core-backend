# Local Ray head: the ingest image's dependencies (ray-jobs/pyproject.toml) plus the trainer's (ray-training/pyproject.toml),
# CPU-only, so one container can run ingest, training and the predictor apps on a Mac. The cluster splits these across a
# CPU worker image and a CUDA one (ray-jobs/Dockerfile, ray-training/Dockerfile); this image exists for docker-compose.yml only.
# Build context ./ray-jobs, the trainer's pyproject.toml arrives as the named context `training` (./ray-training).
ARG BASE
FROM ${BASE}

COPY --chown=ray:users pyproject.toml /tmp/deps/ray-jobs.toml
COPY --from=training --chown=ray:users pyproject.toml /tmp/deps/ray-training.toml
# torch from the CPU index first, then one resolve for everything else; the constraints keep Ray at the base image's version and
# protobuf below 6 (ray[serve] pins protobuf 5.x and an onnx built against protobuf 6 cannot load on it: the install-order version of this fix).
RUN --mount=type=cache,target=/home/ray/.cache/pip,uid=1000,gid=100 \
    python -c "import ray; print('ray==' + ray.__version__)" > /tmp/deps/constraints.txt \
    && echo 'protobuf<6' >> /tmp/deps/constraints.txt \
    && python -c "import tomllib; print('\\n'.join(d for d in tomllib.load(open('/tmp/deps/ray-training.toml', 'rb'))['project']['dependencies'] if d.startswith('torch')))" > /tmp/deps/torch.txt \
    && python -c "import tomllib; deps = {d for f in ('ray-jobs', 'ray-training') for d in tomllib.load(open(f'/tmp/deps/{f}.toml', 'rb'))['project']['dependencies']}; print('\\n'.join(sorted(d for d in deps if not d.startswith('torch'))))" \
        > /tmp/deps/requirements.txt \
    && pip install -c /tmp/deps/constraints.txt --index-url https://download.pytorch.org/whl/cpu -r /tmp/deps/torch.txt \
    && pip install -c /tmp/deps/constraints.txt "ray[train,serve]" -r /tmp/deps/requirements.txt \
    && python -c "import ray.serve, ray.train.torch, torch, transformers, timm, onnx, onnxruntime, boto3, pyarrow, PIL, xgboost, onnxmltools; print('torch', torch.__version__, 'onnx', onnx.__version__)" \
    && rm -rf /tmp/deps
