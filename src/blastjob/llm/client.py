# Thin shim — new code should use llm.providers directly.
# Kept so existing imports in core/ don't break during migration.

from blastjob.llm.providers import detect_provider, make_research_call, make_stream  # noqa: F401
