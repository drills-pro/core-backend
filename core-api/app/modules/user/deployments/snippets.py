"""Call snippets for a deployment (planning/05 "Snippets"): the key is a placeholder, so nothing secret is ever rendered."""
import json

from app.modules.user.deployments.constants import MODEL_ID_HEADER, SNIPPET_KEY_PLACEHOLDER
from app.modules.user.deployments.schemas import DeploymentSnippets

PROJECT_ID_HEADER = "X-Project-Id"


def build(base_url: str, project_id: str, deployment_id: str, body: dict) -> DeploymentSnippets:
    url = f"{base_url.rstrip('/')}/v1/predict"
    headers = {
        "Authorization": f"Bearer {SNIPPET_KEY_PLACEHOLDER}", PROJECT_ID_HEADER: project_id,
        MODEL_ID_HEADER: deployment_id, "Content-Type": "application/json"}
    payload = json.dumps(body)
    curl = f"curl -X POST '{url}' \\\n" + "".join(f"  -H '{k}: {v}' \\\n" for k, v in headers.items()) + f"  -d '{payload}'"
    js = (f"const res = await fetch({json.dumps(url)}, {{\n  method: 'POST',\n  headers: {json.dumps(headers, indent=2)},\n"
          f"  body: JSON.stringify({payload}),\n}});\nconsole.log(await res.json());")
    py = (f"import requests\n\nres = requests.post(\n    {url!r},\n    headers={headers!r},\n    json={body!r},\n)\nprint(res.json())")
    return DeploymentSnippets(curl=curl, javascript=js, python=py)
