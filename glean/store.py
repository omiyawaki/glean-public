"""YAML read/write for records, plus attachment naming policy.

Source of truth is one YAML per record under <library>/records/.
Writes always bump the modified_at field; callers rely on it for index staleness.

See DESIGN.md "Data model" and "Attachment filenames".
"""
