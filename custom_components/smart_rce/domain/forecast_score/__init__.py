"""Forecast scoreboard — PV forecasts and consumption models judged by the decisions they drive.

Two decisions: the T1 reserve (D1, `reserve_score`) and the charge start (D2,
`start_score`). Each decision scores the snapshot it consumed; the production
error splits exactly into a PV part and a consumption part. `day_score` puts a
day together, `ranking` folds final days into the 30-day tables.

Spec: home-assistant-ops `plans/forecast-score-framework.md`.
"""
