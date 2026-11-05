"""The Serve app core-api runs twice (predictor-cpu, predictor-gpu): one Predictor that serves every deployment's model.

core-api POSTs {"model": <ModelSpec>, "input": <the caller's body>} with the deployment's model id in the
`serve_multiplexed_model_id` header. A replica keeps the last `max_models_per_replica` models loaded and loads another on
the first request that names it, so a new deployment or a new run behind a `main` deployment needs no Serve change.
Answers {"predictions": [...], "cold_load": bool}, or {"error": {"code", "message"}} (model.ErrorCode).
"""
import asyncio

from ray import serve
from starlette.requests import Request
from starlette.responses import JSONResponse

from ray_serving import model
from ray_serving.model import ErrorCode, ServeError

ERROR_STATUS = {ErrorCode.BAD_INPUT: 422, ErrorCode.UNKNOWN_FUNCTION: 500, ErrorCode.MODEL_LOAD_FAILED: 500}


def build(args: dict):
    """The application builder named by the Serve config's import_path; args: {"max_models_per_replica": int, "model_base_url": str} (model URLs outside it are refused)."""

    @serve.deployment
    class Predictor:
        def __init__(self):
            self.specs: dict[str, model.ModelSpec] = {}  # the multiplexed loader is given only the id; the request's spec waits here
            self.cold: set[str] = set()

        @serve.multiplexed(max_num_models_per_replica=args["max_models_per_replica"])
        async def get_model(self, model_id: str) -> model.Loaded:
            spec = self.specs.pop(model_id)
            loaded = await asyncio.to_thread(model.load, spec, args["model_base_url"])
            self.cold.add(model_id)
            return loaded

        async def __call__(self, request: Request) -> JSONResponse:
            model_id = serve.get_multiplexed_model_id()
            try:
                body = await request.json()
                self.specs[model_id] = model.ModelSpec.from_dict(body["model"])
                loaded = await self.get_model(model_id)
                predictions = await asyncio.to_thread(model.predict, loaded, body["input"])
            except ServeError as e:
                return JSONResponse({"error": {"code": e.code, "message": str(e)}}, status_code=ERROR_STATUS[e.code])
            except (KeyError, ValueError) as e:  # not JSON, or without "model" / "input"
                return JSONResponse({"error": {"code": ErrorCode.BAD_INPUT, "message": repr(e)}}, status_code=ERROR_STATUS[ErrorCode.BAD_INPUT])
            finally:
                self.specs.pop(model_id, None)
            cold_load = model_id in self.cold
            self.cold.discard(model_id)
            return JSONResponse({"predictions": predictions, "cold_load": cold_load})

    return Predictor.bind()
