# Predict API

Call a deployed model with a project API key. This page describes what the service does today; where a value is a
configuration default it says so.

Examples use `$API` for the service base URL, `$KEY` for a key (`drl_...`), `$PROJECT` for the project id and `$MODEL`
for the model id.

## Authentication

Create a key in the project with the `predict` scope (`POST /v1/projects/{project_id}/api-keys`). A key can be limited to
a list of models and can carry its own `rate_limit_per_minute`. The secret is shown once.

Every request sends:

| Header | Required | Meaning |
|---|---|---|
| `Authorization: Bearer drl_...` | yes | the key |
| `X-Project-Id` | yes | the project the key belongs to; a different project is the same `401` as a bad key |
| `X-Model-Id` | yes on `POST /v1/predict` | the model to call; on the other endpoints send it too when the key is limited to some models |
| `X-Model-Revision` | no | the model revision (version number); without it the newest live deployment of the model answers |
| `Idempotency-Key` | no (required with `Prefer: respond-async`) | 1 to 255 printable ASCII characters |
| `Prefer: respond-async` | no | run in the background, see below |
| `Accept` | no | `application/json` (or `*/*`); `text/event-stream` alone is refused, see Streaming |

## Call a model

`POST /v1/predict`, body is a JSON object with exactly two possible keys:

| Key | Meaning |
|---|---|
| `input` | required, any JSON value. Sent to the model unchanged; its shape is the model's own |
| `metadata` | optional JSON object, at most 4096 bytes of JSON. Echoed back, never sent to the model |

Any other top-level key is a `422`.

```bash
curl -sS -X POST "$API/v1/predict" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Project-Id: $PROJECT" \
  -H "X-Model-Id: $MODEL" \
  -H "Content-Type: application/json" \
  -d '{"input": {"image": "<base64 image>"}, "metadata": {"trace": "t1"}}'
```

A successful call answers `200` with the envelope (the `input` above is only an example; send what your model expects):

```json
{
  "id": "pred_0b0f6c0e-6f43-4a31-9d0a-3f6f2f8e5c11",
  "status": "completed",
  "model": {"id": "<model id>", "revision": "3"},
  "output": {"predictions": [{"label": "cat"}]},
  "usage": {"latency_ms": 12.5},
  "metadata": {"trace": "t1"},
  "error": null,
  "expired": false,
  "created_at": "2026-11-10T09:30:00Z"
}
```

`status` is one of `queued`, `in_progress`, `completed`, `failed`, `cancelled`. A sync answer is always `completed`.
`error` is `{"code", "detail"}` on a failed background prediction. `expired` is true once the stored input and output were
purged (see Retention). Each successful call is metered once.

## Errors

Errors are `application/problem+json`:

```json
{"type": "urn:drills:problem:rate_limited", "title": "Too Many Requests", "status": 429,
 "detail": "rate limit exceeded (60/min for this model)", "code": "rate_limited", "request_id": "..."}
```

Quote `request_id` (also the `X-Request-Id` response header) when you contact support. A `404` always has the body
`not found` and the code `not_found`.

| Status | `code` | When |
|---|---|---|
| 401 | `invalid_api_key` | key missing, unknown, revoked, expired, or `X-Project-Id` does not match. One body for all of them |
| 403 | `forbidden` | the key lacks the `predict` scope (`missing scope predict`) or may not use that model |
| 404 | `not_found` | unknown model or revision, a model of another project, a model with no deployment, an `X-Model-Id` that is not a model id, or a prediction that is not yours |
| 409 | `deployment_not_active` | the model's deployment is deploying, stopped or failed |
| 409 | `no_servable_model` | the deployment's training run has no servable model |
| 409 | `request_in_progress` | a retry of a sync call with the same `Idempotency-Key` whose first request is still running; has `Retry-After: 1` |
| 413 | `body_too_large` | the body is over the service ceiling (10 MiB by default) or over your plan's body limit |
| 422 | `validation_error` | a missing `X-Model-Id`, an unknown top-level key, bad `metadata`, a malformed header; `detail` and `errors` say which; also a background call without `Idempotency-Key` |
| 422 | `body_not_json`, `body_not_object` | the body is not JSON, or not a JSON object |
| 422 | `model_rejected_input` | the model refused the input; `detail` is the model's message cut to 500 characters |
| 422 | `idempotency_key_reuse` | the `Idempotency-Key` was used with a different request |
| 406 | `not_acceptable` | `Accept: text/event-stream` and nothing that takes JSON |
| 429 | `rate_limited` | a rate limit, with `Retry-After` |
| 429 | `too_many_inflight` | too many concurrent calls for this deployment on one service replica |
| 502 | `upstream_error` | the model could not answer. Its own message is never returned |
| 503 | `tenant_suspended` | your account is not active |
| 504 | `upstream_timeout` | the model did not answer in time |

For a background prediction the failure is stored instead: `GET` answers `200` with `status: "failed"` and `error`
holding the same `code` and `detail`.

## Idempotency

Send an `Idempotency-Key` to make a call safe to retry. The key is scoped to your project.

- Same key and same request (input, metadata, model and revision) returns what the first request made: no second model call
  and no second usage charge. A sync call that was stored answers `200`; a background one answers `202` until it finishes.
- Same key and a different request is `422 idempotency_key_reuse`.
- A failure that came from the model (`model_rejected_input`, `upstream_error`, `upstream_timeout`) is stored and the same
  error is returned on a retry. Any other failure (validation, rate limit, not active, ...) does not use up the key, so
  the retry runs normally.
- If the first sync request is still running, a retry waits up to 10 seconds (default) for it, then answers
  `409 request_in_progress`. If the first request never finished, the retry is answered with `502 upstream_error` and the
  key is spent: send a new key to run it again.
- A sync call with a key is replayable for 24 hours (default).

A sync call without a key is not stored and cannot be fetched later.

## Background mode

Send `Prefer: respond-async` and an `Idempotency-Key`. The call is queued and answers `202` at once with
`Location: /v1/predict/{id}`, `Preference-Applied: respond-async` and an envelope with `status: "queued"`.

```bash
curl -sS -i -X POST "$API/v1/predict" \
  -H "Authorization: Bearer $KEY" -H "X-Project-Id: $PROJECT" -H "X-Model-Id: $MODEL" \
  -H "Idempotency-Key: order-1234" -H "Prefer: respond-async" \
  -H "Content-Type: application/json" \
  -d '{"input": {"image": "<base64 image>"}}'
```

A `5xx`, timeout or lost worker is retried up to 3 attempts in total (default) with growing waits. A rejection by the
model (`422`) fails the prediction at once.

A key sees only the predictions it created, in its own project. Anything else is `404`.

### Fetch

```bash
curl -sS "$API/v1/predict/$ID" \
  -H "Authorization: Bearer $KEY" -H "X-Project-Id: $PROJECT"
```

Returns the envelope; poll until `status` is `completed`, `failed` or `cancelled`. For a background prediction `usage`
also holds `attempt`.

### Cancel

```bash
curl -sS -X POST "$API/v1/predict/$ID/cancel" \
  -H "Authorization: Bearer $KEY" -H "X-Project-Id: $PROJECT"
```

A `queued` prediction becomes `cancelled` immediately. An `in_progress` background prediction is marked for
cancellation and ends as `cancelled` when the worker stops its call (the response still shows `in_progress`). A finished
prediction is returned unchanged with `200`: cancelling twice is safe.

### Events

```bash
curl -sS -N "$API/v1/predict/$ID/events" \
  -H "Authorization: Bearer $KEY" -H "X-Project-Id: $PROJECT" \
  -H "Last-Event-ID: 2"
```

A `text/event-stream` of status changes. Each event has `id` (a sequence number), `event` (the status) and `data` (JSON;
the envelope on the final event, which carries the output). The stream ends after the first terminal event. Send
`Last-Event-ID` to receive only the events after that number; a value that is not a number replays from the start. A quiet
stream sends a `: heartbeat` comment every 15 seconds.

```
id: 3
event: completed
data: {"id": "pred_...", "status": "completed", "output": {...}, ...}
```

## Streaming

Model output streaming is not available. `Accept: text/event-stream` (without `application/json`, `application/*` or `*/*`)
on `POST /v1/predict` answers `406 not_acceptable`. Use background mode and the events endpoint to follow a prediction.

## Rate limits

Each call counts against three fixed one-minute windows, checked in this order: the key's own `rate_limit_per_minute`
(when the key has one), your plan's limit, and the model's deployment limit. The first one over its limit answers
`429 rate_limited`, and the `detail` names which (`this key`, `this plan`, `this model`).

| Plan | Per minute |
|---|---|
| free | 10 |
| pro | 120 |
| enterprise | 1000 |

The deployment limit is 60 per minute unless it was changed on the deployment. A background call counts once, when it is
accepted. A rejected call is not stored and does not use up an `Idempotency-Key`.

`Retry-After` is the number of seconds until the current one-minute window ends, then the count starts again:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 23
Content-Type: application/problem+json
```

## Payload size

- The body limit is 10 MiB by default; your plan may set a lower one. Over it is `413 body_too_large`. The declared
  `Content-Length` is checked before the body is read.
- A stored prediction (a background one, or a sync one with an `Idempotency-Key`) keeps an input or output whose JSON is
  over 256 KB in object storage instead of in the database. You see no difference: `GET` and the events return the full
  `output` inline, however large.

## Retention

A finished background prediction keeps its input and output for 7 days (default); a stored sync prediction for 24 hours.
After that `GET` still answers `200` with the id, status and timestamps, `output: null` and `expired: true`.

## Legacy route

`POST /v1/predict/{deployment_id}` with an `X-Predict-Key` header (a per-deployment key, not a `drl_` key) still works
and answers as before, but is deprecated. Every response (errors included) carries `Deprecation: true` and
`Sunset: <date>` (31 Dec 2026 23:59:59 GMT by default). Move to `POST /v1/predict` with a project API key and
`X-Model-Id`. The OpenAPI document does not set the `deprecated` flag on this route; the headers are the signal.
