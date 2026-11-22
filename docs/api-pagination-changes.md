# Cursor pagination changes

The list endpoints below now use keyset pagination. `limit`/`offset` are replaced by
`page_size` and `page_token`, and the response body is `{"items": [...], "next_page_token": ...}`
instead of a bare array. The filtered total is still available in `X-Total-Count`.

| Endpoint | Ordering |
| --- | --- |
| `GET /v1/admin/activity` | `created_at DESC, id DESC` |
| `GET /v1/admin/audit-log` | `created_at DESC, id DESC` |
| `GET /v1/admin/models` | `created_at DESC, id DESC` |
| `GET /v1/admin/models/{version_id}/versions` | `created_at DESC, id DESC` |
| `GET /v1/admin/tenants` | `created_at DESC, id DESC` |
| `GET /v1/teams` | `created_at ASC, id ASC` |
| `GET /v1/projects/{project_id}/training-runs` | `created_at DESC, id DESC` |
| `GET /v1/projects/{project_id}/usage/events` | `occurred_at DESC, id DESC` |

Callers start with no `page_token`, then pass `next_page_token` from the previous response.
A `page_token` that cannot be decoded returns `400 invalid_page_token`. `page_size` defaults
to 50 and is capped at 200. An absent `next_page_token` means the last page was reached.

Clients must read `response.items` rather than treating the response as an array. Generated
OpenAPI types and the test console/customer console have been updated accordingly.
