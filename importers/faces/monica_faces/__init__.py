"""Find faces in Monica's post photos and avatars, and suggest who they are for review.

Images are fetched over signed requests, decoded in memory and never written to disk. Each face keeps
a small crop and its embedding in a local SQLite file; only boxes, quality and suggestions go back.
"""
