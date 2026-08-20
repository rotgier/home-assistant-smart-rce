"""Deposit bounded context — prosumer net-billing settlement and PV savings.

Analytical/reporting context (ADR-025), deliberately separate from `ems`: it
recomputes a rolling history once a day instead of steering anything live.
Never imports `ems` — the only overlap (hourly RCE prices) crosses through an
application port wired at factory level.
"""
