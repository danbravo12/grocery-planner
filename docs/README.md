# Documentation

| Document | What it is | Read it when |
| --- | --- | --- |
| [Design document](Multi-Agent%20Grocery%20Planner%20Design%20Document.md) | What the system is and why: the agents, the decision rule, guardrails, evals, the API, configuration, limitations. Matches what was built; the history is in DECISIONS.md | you want the whole design in one place |
| [DECISIONS.md](DECISIONS.md) | The log of every choice the design left open and everything that broke, step by step (steps 1 to 15b), with numbers. Has a table of contents | you want to know why something is the way it is |
| [NUMBERS.md](NUMBERS.md) | Every figure quoted in the README, with the query or source and the date measured | you want to check a number |
| [DEPLOY.md](DEPLOY.md) | The Cloud Run runbook: every `gcloud` command and why, rollback, rotating a secret, costs, the smoke test | you deploy, roll back or rotate a secret |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Running, testing and extending locally: setup, the API, the CLI, the database, the evals, tracing | you work on the code |
| [screenshots/](screenshots) | The images the README uses (made by `frontend/scripts/hero-shot.mjs` and `screenshots.mjs`) | |

Other places: the [README](../README.md) is the front page, [`.env.example`](../.env.example) lists every setting, [`db/seed/EDGE_CASES.md`](../db/seed/EDGE_CASES.md) describes the deliberate test cases in the seeded prices, and [`backend/evals/cases`](../backend/evals/cases) holds the eval cases.
