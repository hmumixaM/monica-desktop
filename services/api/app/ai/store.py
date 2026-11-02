"""Stored model outputs, regenerated only when their input changes."""

import json
from collections.abc import Callable

from pydantic import BaseModel
from sqlalchemy import text

from .client import input_hash, structured


def current(db, kind: str, subject: str) -> dict | None:
    row = db.execute(text("SELECT input_hash, output, generated_at FROM ai_outputs WHERE kind = :kind AND subject = :subject"),
                     {"kind": kind, "subject": subject}).mappings().one_or_none()
    return dict(row) if row else None


def outputs(db, kind: str) -> dict[str, dict]:
    return {row["subject"]: {**row["output"], "generated_at": row["generated_at"]} for row in db.execute(
        text("SELECT subject, output, generated_at FROM ai_outputs WHERE kind = :kind"), {"kind": kind}).mappings()}


def generate(db, kind: str, subject: str, model: str, system: str, payload: dict,
             schema: type[BaseModel], call: Callable = structured) -> bool:
    """Call the model unless the stored output came from the same input. Returns whether it called.

    Commits before the call so no transaction stays open while the model thinks.
    """
    digest = input_hash(model, system, payload)
    stored = current(db, kind, subject)
    if stored and stored["input_hash"] == digest:
        return False
    db.commit()
    result = call(model, system, payload, schema)
    db.execute(text("""INSERT INTO ai_outputs(kind, subject, input_hash, model, output) VALUES(:kind, :subject, :hash, :model, CAST(:output AS jsonb))
                       ON CONFLICT (kind, subject) DO UPDATE SET input_hash = EXCLUDED.input_hash, model = EXCLUDED.model,
                         output = EXCLUDED.output, generated_at = now()"""),
               {"kind": kind, "subject": subject, "hash": digest, "model": model,
                "output": json.dumps(result.model_dump(), ensure_ascii=False)})
    return True
