// Regenerates src/api/schema.d.ts from core-api's OpenAPI spec (same as customer-frontend's script).
// The spec comes from VITE_API_URL/openapi.json (default production); API_SPEC_URL overrides it with a URL or a spec file path,
// e.g. one dumped from this repo: `cd ../core-api && APP_ENV=test .venv/bin/python -c "import json; from app.main import app; print(json.dumps(app.openapi()))" > /tmp/spec.json`.
// The spec lists `authorization` as a header on every route; the client adds it in middleware, so drop it here.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

try {
  process.loadEnvFile('.env.local');
} catch {
  /* no .env.local */
}
const url = process.env.API_SPEC_URL ?? `${process.env.VITE_API_URL ?? 'https://api.drills.pro'}/openapi.json`;
let spec;
if (/^https?:/.test(url)) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  spec = await res.json();
} else spec = JSON.parse(readFileSync(url, 'utf8'));

const isAuth = (p) => p.in === 'header' && p.name.toLowerCase() === 'authorization';
for (const item of Object.values(spec.paths))
  for (const op of Object.values(item)) if (Array.isArray(op?.parameters)) op.parameters = op.parameters.filter((p) => !isAuth(p));

const file = join(mkdtempSync(join(tmpdir(), 'drills-api-')), 'openapi.json');
writeFileSync(file, JSON.stringify(spec));
const out = 'src/api/schema.d.ts';
execFileSync('pnpm', ['exec', 'openapi-typescript', file, '-o', out], { stdio: 'inherit' });
writeFileSync(out, readFileSync(out, 'utf8').replaceAll('—', '-'));
