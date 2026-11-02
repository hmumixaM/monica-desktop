"""Structured completions from LiteLLM through the OpenAI SDK."""

import json
import os
from functools import lru_cache
from hashlib import sha256
from typing import TypeVar

from openai import APIConnectionError, APIStatusError, OpenAI
from pydantic import BaseModel
from sqlalchemy import text

from ..config import get_settings
from ..job_queue import checkpoint, interruptible_wait

Output = TypeVar("Output", bound=BaseModel)


@lru_cache
def client() -> OpenAI:
    settings = get_settings()
    return OpenAI(base_url=settings.litellm_base_url, api_key=settings.litellm_api_key, timeout=180, max_retries=0)


def input_hash(model: str, system: str, payload: dict) -> str:
    """What the model would see; equal hashes mean the stored output is still current."""
    return sha256(json.dumps([model, system, payload], ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def reserve_call(model):
    """Rate limit actual call starts across workers, without sleeping in a transaction."""
    from ..db import engine
    from ..job_queue import current_job
    kind = (current_job() or {}).get('job_type', '')
    defaults = {'ai.reconnect_one': 30}
    setting = 'AI_' + kind.removeprefix('ai.').upper() + '_CALL_SPACING_SECONDS'
    spacing = max(0, int(os.environ.get(setting, os.environ.get('AI_CALL_SPACING_SECONDS', str(defaults.get(kind, 12))))))
    with engine.begin() as db:
        wait = db.execute(text("""INSERT INTO model_call_slots(name,next_at)
            VALUES(:model,clock_timestamp()+make_interval(secs=>:spacing))
            ON CONFLICT(name) DO UPDATE SET next_at=greatest(clock_timestamp(),model_call_slots.next_at)
              +make_interval(secs=>:spacing)
            RETURNING greatest(0,extract(epoch FROM next_at-clock_timestamp())-:spacing)"""),
                          {"model": model, "spacing": spacing}).scalar_one()
    interruptible_wait(float(wait))


def _complete(model, **kwargs):
    for attempt in range(3):
        checkpoint()
        reserve_call(model)
        checkpoint()
        try:
            return client().chat.completions.parse(model=model, **kwargs)
        except (APIConnectionError, APIStatusError) as exc:
            if attempt == 2 or isinstance(exc, APIStatusError) and exc.status_code not in (408, 429) and exc.status_code < 500:
                raise
    raise RuntimeError('Unreachable completion retry state')


def structured(model: str, system: str, payload: dict, schema: type[Output]) -> Output:
    completion = _complete(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        response_format=schema,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError(f"{model} returned no parsable {schema.__name__}")
    return parsed


def vision(model: str, system: str, prompt: str, images: list[tuple[str, str]], schema: type[Output]) -> Output:
    """Like `structured`, with labelled images (label, data URL) after the prompt, each preceded by its label."""
    content = [{"type": "text", "text": prompt}]
    for label, url in images:
        content += [{"type": "text", "text": label}, {"type": "image_url", "image_url": {"url": url}}]
    completion = _complete(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
        response_format=schema,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise ValueError(f"{model} returned no parsable {schema.__name__}")
    return parsed
