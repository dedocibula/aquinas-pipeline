"""Persistence layer: DB connection, typed persistence models, and repositories.

- ``storage.db`` — connection/context manager and source/work lookups.
- ``storage.models`` — pure dataclasses returned by the repositories (a leaf
  module: it imports nothing else in the pipeline, so the repositories can depend
  on it without an import cycle).
- ``storage.repositories`` — all SQL, behind GlossaryRepository / SegmentRepository
  / TermUsageRepository / RunRepository.
"""
