"""Garden bounded context — mowing (Luba), and later rain/irrigation/well.

Hosted inside the smart_rce integration but isolated from the `ems` context:
`garden/domain` never imports `ems`. See ADR-024 (home-assistant-ops) +
plans/garden-module.md for the architecture rationale.
"""
