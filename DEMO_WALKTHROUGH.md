# Three-minute demo walkthrough

## 0:00–0:30 — Fleet view

Open `/fleet`. Point out the five synthetic tails, health scores, prioritized low-health list, and colored Three.js component illustration. The status stream refreshes live; in demo mode one tail receives a synthetic fault every 30 seconds.

## 0:30–1:00 — Fault to component

Run `python -m backend.scripts.show_snag_mappings`. Show the noisy free-text snag examples, mapped system/component, and similarity score. Open `/graph/aircraft/TAIL-101` to show how the graph joins components, snags, work orders, and spares.

## 1:00–1:40 — Plan the work

Use the date comparison in the what-if panel and show the availability delta. Open the spares panel, then request work-order bundling from an analyst or admin view. Explain that the KPIs are deterministic synthetic comparisons, not measured fleet outcomes.

## 1:40–2:20 — Copilot

Ask “Oil pressure fluctuates during climb; what should I check?” Show the cited manual and similar historical case IDs. Local TF-IDF retrieval works without a model service; Ollama or an API model can be selected through environment variables.

## 2:20–3:00 — Prognostics and wrap-up

Show the `/predict/rul`, `/predict/anomaly`, and `/predict/metrics` contracts. Training and test-set RMSE/NASA score require the official FD001 train, test, and RUL text files under `data/CMAPSSData/`. This project mirror has no such files, so do not present placeholder test results. End with the API docs at `/docs` and the reproducible demo/test commands in the README.
