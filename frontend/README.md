# Frontend

React 19, Vite, TypeScript (strict), Tailwind 4 and react-router. See the repository README for how it fits together.

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api to http://localhost:8000
npm run build      # frontend/dist, which FastAPI serves itself (same origin)
npm run typecheck && npm run lint && npm test
npm run e2e        # Playwright smoke test, every /api call mocked (needs Chrome, or run `npx playwright install chromium`)
npm run gen:types  # regenerate src/api/types.ts from the FastAPI OpenAPI schema
```

`src/api/types.ts` is generated; CI fails if it is stale. `node scripts/screenshots.mjs <base-url> <dir>` makes real plans and saves screenshots.
