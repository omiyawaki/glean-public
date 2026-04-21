"""SQLite FTS5 index, derived from the YAML records.

Staleness is detected by comparing each YAML's modified_at against the
stored yaml_modified column. See DESIGN.md "SQLite index schema".
"""
