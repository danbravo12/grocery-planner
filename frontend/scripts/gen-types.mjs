// Regenerates src/api/types.ts from the FastAPI OpenAPI schema (the single source of truth for
// the request and response shapes). CI runs this and fails if the committed file differs.
import { execFileSync, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = resolve(fileURLToPath(import.meta.url), '..')
const frontend = resolve(here, '..')
const backend = resolve(frontend, '..', 'backend')

const hasUv = spawnSync('uv', ['--version'], { stdio: 'ignore' }).status === 0
const [cmd, args] = hasUv
  ? ['uv', ['run', 'python', '-m', 'grocery_planner.api.openapi']]
  : [join(backend, '.venv', 'bin', 'python'), ['-m', 'grocery_planner.api.openapi']]
if (!hasUv && !existsSync(cmd)) {
  console.error('Need uv, or a backend/.venv with the backend installed, to read the OpenAPI schema.')
  process.exit(1)
}

const schema = execFileSync(cmd, args, { cwd: backend, encoding: 'utf8', maxBuffer: 1 << 26 })
const dir = mkdtempSync(join(tmpdir(), 'gp-openapi-'))
const file = join(dir, 'openapi.json')
writeFileSync(file, schema)
try {
  execFileSync(
    join(frontend, 'node_modules', '.bin', 'openapi-typescript'),
    [file, '-o', join(frontend, 'src', 'api', 'types.ts')],
    { stdio: 'inherit' },
  )
} finally {
  rmSync(dir, { recursive: true, force: true })
}
