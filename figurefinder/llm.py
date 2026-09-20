"""
Everything that talks to a language model: plan, score, judge.

Provider-agnostic. OpenAI is the default; Anthropic still works if you set
FF_PROVIDER=anthropic or pass --provider anthropic. The rubric and the scoring
maths are identical either way, so you can A/B the two on the same topic.
"""

from __future__ import annotations

import base64
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from . import config, rubric, selection
from .search import Candidate, domain_bonus


# ---------------------------------------------------------------------------
# Neutral content blocks — each provider translates these into its own shape.
# ---------------------------------------------------------------------------

def text_block(s: str) -> dict:
    return {"kind": "text", "text": s}


def image_block(path: str) -> dict:
    with open(path, "rb") as f:
        return {"kind": "image", "b64": base64.standard_b64encode(f.read()).decode()}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class ProviderError(RuntimeError):
    pass


class OpenAIProvider:
    name = "openai"
    env_var = "OPENAI_API_KEY"

    def __init__(self):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ProviderError(
                "The openai package isn't installed. Run: pip install openai"
            ) from e
        self._client = OpenAI()

    def _content(self, blocks: List[dict]) -> list:
        out = []
        for b in blocks:
            if b["kind"] == "text":
                out.append({"type": "text", "text": b["text"]})
            else:
                out.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b['b64']}",
                        # "low" downsamples to 512px, which destroys exactly the
                        # small label text we need to judge legibility on.
                        "detail": config.OPENAI_IMAGE_DETAIL,
                    },
                })
        return out

    def structured(self, model: str, system: str, blocks: List[dict],
                   schema: dict, schema_name: str, max_tokens: int) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._content(blocks)},
        ]
        kwargs = dict(
            model=model,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        )
        try:
            resp = self._client.chat.completions.create(
                max_completion_tokens=max_tokens, **kwargs
            )
        except Exception as e:
            # Older models still want the deprecated parameter name.
            if "max_completion_tokens" not in str(e):
                raise
            resp = self._client.chat.completions.create(max_tokens=max_tokens, **kwargs)

        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise ProviderError(f"model refused: {msg.refusal}")
        if resp.choices[0].finish_reason == "length":
            raise ProviderError(
                "response was cut off before the JSON closed — lower "
                "SCORE_BATCH_SIZE in config.py or raise max_tokens"
            )
        return json.loads(msg.content)


class AnthropicProvider:
    name = "anthropic"
    env_var = "ANTHROPIC_API_KEY"

    def __init__(self):
        try:
            import anthropic
        except ImportError as e:
            raise ProviderError(
                "The anthropic package isn't installed. Run: pip install anthropic"
            ) from e
        self._client = anthropic.Anthropic()

    def _content(self, blocks: List[dict]) -> list:
        out = []
        for b in blocks:
            if b["kind"] == "text":
                out.append({"type": "text", "text": b["text"]})
            else:
                out.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg",
                               "data": b["b64"]},
                })
        return out

    def structured(self, model: str, system: str, blocks: List[dict],
                   schema: dict, schema_name: str, max_tokens: int) -> dict:
        # Forcing a tool call is Anthropic's equivalent of strict JSON schema.
        tool = {"name": schema_name, "description": f"Submit {schema_name}.",
                "input_schema": schema}
        msg = self._client.messages.create(
            model=model, max_tokens=max_tokens, system=system, tools=[tool],
            tool_choice={"type": "tool", "name": schema_name},
            messages=[{"role": "user", "content": self._content(blocks)}],
        )
        for block in msg.content:
            if block.type == "tool_use" and block.name == schema_name:
                return block.input
        raise ProviderError(f"model did not return {schema_name} (stop: {msg.stop_reason})")


_PROVIDERS = {"openai": OpenAIProvider, "anthropic": AnthropicProvider}
_cache: dict = {}

# Which key each provider needs. Kept here so the CLI can check for it *before*
# constructing a client — the SDKs raise their own noisy error otherwise.
PROVIDER_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def get_provider(name: Optional[str] = None):
    name = (name or config.PROVIDER).lower()
    if name not in _PROVIDERS:
        raise ProviderError(f"unknown provider {name!r}; pick one of {list(_PROVIDERS)}")
    if name not in _cache:
        try:
            _cache[name] = _PROVIDERS[name]()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"could not start the {name} client: {e}") from e
    return _cache[name]


def models_for(provider_name: str) -> dict:
    # The CLI loads .env after importing config; read overrides at call time.
    return {stage: os.environ.get(f"FF_{stage.upper()}_MODEL", default)
            for stage, default in config.MODELS[provider_name].items()}


# ---------------------------------------------------------------------------
# 1. Plan
# ---------------------------------------------------------------------------

def make_plan(notes: str, audience: str, style: str, language: str,
              n_queries: int = config.MAX_QUERIES, provider=None,
              model: Optional[str] = None) -> dict:
    p = provider or get_provider()
    return p.structured(
        model=model or models_for(p.name)["planner"],
        system=rubric.PLANNER_SYSTEM,
        blocks=[text_block(rubric.PLANNER_PROMPT.format(
            notes=notes.strip(), audience=audience, style=style,
            language=language, n_queries=n_queries,
        ))],
        schema=rubric.PLAN_SCHEMA,
        schema_name="submit_plan",
        max_tokens=2000,
    )


# ---------------------------------------------------------------------------
# 2. Score
# ---------------------------------------------------------------------------

def _resolution_points(c: Candidate) -> float:
    """Deterministic 0-1 resolution adequacy. The model can't see this."""
    if c.is_vector:
        return 1.0          # vector art scales to any slide size
    w = c.real_width or c.width
    if w <= 0:
        return 0.4
    if w >= config.RESOLUTION_IDEAL_WIDTH:
        return 1.0
    if w <= config.RESOLUTION_FLOOR_WIDTH:
        return 0.15
    span = math.log(config.RESOLUTION_IDEAL_WIDTH / config.RESOLUTION_FLOOR_WIDTH)
    return 0.15 + 0.85 * math.log(w / config.RESOLUTION_FLOOR_WIDTH) / span


def compute_final_score(c: Candidate) -> float:
    """Weighted criteria + resolution, normalised to 0-100, then veto multipliers."""
    total_weight = sum(cr.weight for cr in rubric.CRITERIA) + config.RESOLUTION_WEIGHT
    earned = sum(
        cr.weight * (c.scores.get(cr.key, 0) / cr.max_score) for cr in rubric.CRITERIA
    )
    earned += config.RESOLUTION_WEIGHT * _resolution_points(c)
    score = 100.0 * earned / total_weight

    for name, spec in rubric.VETO_FLAGS.items():
        if c.flags.get(name):
            if spec["multiplier"] == 0:
                return 0.0
            score *= spec["multiplier"]

    score += domain_bonus(c.host) * 0.25   # a nudge, never a rescue
    return round(min(score, 100.0), 1)


def score_candidates(candidates: List[Candidate], plan: dict, audience: str,
                     style: str, language: str, provider=None,
                     model: Optional[str] = None, verbose=True) -> List[Candidate]:
    p = provider or get_provider()
    model = model or models_for(p.name)["scorer"]
    schema = rubric.build_score_schema()
    prompt = rubric.render_scorer_prompt(plan, audience, style, language)
    by_id = {c.id: c for c in candidates}

    batches = [
        candidates[i:i + config.SCORE_BATCH_SIZE]
        for i in range(0, len(candidates), config.SCORE_BATCH_SIZE)
    ]

    def grade_batch(batch):
        blocks = [text_block(prompt)]
        for c in batch:
            hint = f" — page title: {c.title}" if c.title else ""
            blocks.append(text_block(
                f"\n--- CANDIDATE {c.id} (source: {c.host}{hint}) ---"))
            blocks.append(image_block(c.local_path))

        return p.structured(
            model=model, system=rubric.SCORER_SYSTEM, blocks=blocks,
            schema=schema, schema_name="submit_scores", max_tokens=6000,
        )

    def apply_scores(data):
        for row in data.get("candidates", []):
            c = by_id.get(row.get("id"))
            if not c:
                continue
            c.scores = {cr.key: int(row.get(cr.key, 0)) for cr in rubric.CRITERIA}
            c.flags = {k: bool(row.get(k)) for k in rubric.VETO_FLAGS}
            c.elements_found = row.get("elements_found") or []
            c.elements_missing = row.get("elements_missing") or []
            c.verdict = row.get("verdict", "")
            c.caveat = row.get("caveat") or None
            c.final_score = compute_final_score(c)
            c.rejected = selection.rejection_reason(c)

    completed = 0
    workers = min(config.SCORE_WORKERS, len(batches))
    if workers <= 1:
        for batch in batches:
            apply_scores(grade_batch(batch))
            completed += len(batch)
            if verbose:
                print(f"    scored {completed}/{len(candidates)}")
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vision-score") as pool:
            pending = {pool.submit(grade_batch, batch): batch for batch in batches}
            for future in as_completed(pending):
                batch = pending[future]
                apply_scores(future.result())
                completed += len(batch)
                if verbose:
                    print(f"    scored {completed}/{len(candidates)}")

    scored = [c for c in candidates if c.scores]
    scored.sort(key=lambda c: c.final_score, reverse=True)
    return scored


# ---------------------------------------------------------------------------
# 3. Judge (optional head-to-head on the finalists)
# ---------------------------------------------------------------------------

def judge(finalists: List[Candidate], plan: dict, provider=None,
          model: Optional[str] = None) -> Optional[dict]:
    if len(finalists) < 2:
        return None
    p = provider or get_provider()
    blocks = [text_block(rubric.JUDGE_PROMPT.format(
        concept=plan.get("concept", ""),
        must_show="\n".join(f"- {m}" for m in plan.get("must_show", [])),
    ))]
    for c in finalists:
        blocks.append(text_block(f"\n--- CANDIDATE {c.id} ---"))
        blocks.append(image_block(c.local_path))

    return p.structured(
        model=model or models_for(p.name)["judge"],
        system=rubric.SCORER_SYSTEM, blocks=blocks,
        schema=rubric.PICK_SCHEMA, schema_name="submit_pick", max_tokens=1000,
    )
