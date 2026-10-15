# Seeded edge cases

Dates in the seed are relative to an **anchor date** (`price_age_days`, `special_weeks`). The loader resolves them against today in Australia/Sydney by default; tests and evals fix the anchor at **2026-10-14 (Wednesday)**, and the concrete dates below assume that anchor. Specials run **Wednesday to Tuesday** (Coles and Woolworths both reset on Wednesday): `special_weeks: 1` ends on the Tuesday of the sale week containing the anchor (2026-10-20), `2` on the Tuesday after (2026-10-27). Every special also has a `regular_price`, the shelf price once it is over.
Prices are fictional. `tests/test_seed_edge_cases.py` asserts every row below.

| # | Edge case | Product IDs | Detail |
| --- | --- | --- | --- |
| a1 | Item at Woolworths only | `wol-sourdough-01` (no Coles product) | "sourdough" returns nothing at Coles |
| a2 | Item at Coles only | `col-potato-gems-01` (no Woolworths product) | "potato gems" returns nothing at Woolworths |
| b1 | Purchased quantities within 15% across stores | `col-spaghetti-01` (500 g), `wol-spaghetti-01` (450 g) | Needing 400 g: Coles buys one 500 g pack, Woolworths one 450 g pack (11% apart), so no `pack_gap` |
| b2 | Purchased quantities beyond 15% across stores | `col-yoghurt-01` (1000 g), `wol-yoghurt-01` (700 g) | Needing 1 kg: Coles buys 1000 g, Woolworths two packs = 1400 g (40% apart), so a `pack_gap` flag. The tier stays equivalent |
| c | Special ending at the end of the sale week | `wol-chicken-breast-01` | $8.50 on special (regular $11.50), `special_weeks: 1`: ends Tue 2026-10-20 for the test anchor (6 days out; the sale week runs Wed 2026-10-14 to Tue 2026-10-20) |
| d | Near-identical basket (under 2%) | Coles: `col-milk-01`, `col-white-bread-01`, `col-eggs-01`, `col-banana-01`, `col-brown-onion-01`, `col-beef-mince-01`, `col-cheddar-01`, `col-carrot-01`. Woolworths: same slugs with `wol-` prefix | One pack of each. Coles $34.80 vs Woolworths $34.50 (0.86% apart). None are on special |
| e | Branded product at both stores | `col-tomato-sauce-02`, `wol-tomato-sauce-02` | Bellvue Tomato Sauce 500ml at both, for exact-brand matching |
| f | Popular brand on special cheaper than house brand | `col-bacon-02` vs `col-bacon-01` | Kingsway Streaky Bacon 250g on special at $4.50 (regular $7.00, above the house brand's $5.50), `special_weeks: 1`: ends Tue 2026-10-20. Same size and band as the house brand |

Pack size never changes a match tier. Purchased quantity is `packs x pack_size`; the gap is measured relative to the smaller quantity. Note the same spaghetti needing 500 g is a gap (Woolworths must buy two 450 g packs = 900 g).

Other useful rows (not edge cases, but present): `col-fries-02` is a second-week special (`special_weeks: 2`, ends Tue 2026-10-27): Kingsway Straight Cut Fries 1kg at $3.00, regular $4.50; `col-rice-01` and others are `budget` house brands against `standard` branded products, which exercises the tier-difference flag.

## Specials and their regular prices

| Product | Special | Regular | Ends (anchor 2026-10-14) |
| --- | ---: | ---: | --- |
| `wol-chicken-breast-01` | $8.50 | $11.50 | Tue 2026-10-20 (`special_weeks: 1`) |
| `col-bacon-02` | $4.50 | $7.00 | Tue 2026-10-20 (`special_weeks: 1`) |
| `col-fries-02` | $3.00 | $4.50 | Tue 2026-10-27 (`special_weeks: 2`) |
