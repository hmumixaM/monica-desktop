"""Model features through the NAS LiteLLM proxy.

Data boundary: payloads use current profile facts, hand-kept notes, saved visual readings and
aggregate counts. `network_context` reads focused social interaction columns without bodies.
Only profile suggestions read permitted chat excerpts, through `chat_excerpts`.
"""
