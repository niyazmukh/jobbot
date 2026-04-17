# Discovery Fixtures

This directory holds deterministic fixture inputs for discovery adapters.

Rules:
- keep source-family fixtures separated by provider folder
- store raw payloads or HTML snapshots exactly as captured where practical
- add a short `notes.md` file when a fixture has quirks that affect parsing
- prefer small, representative samples over giant dumps

Current fixture families:
- `google/`
- `greenhouse/`
- `lever/`
- `meta/`
- `microsoft/`
- `workday/`

Planned:
- `linkedin/`
