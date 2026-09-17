"""
Pydantic harness for open-typesafe-camoufox — 2 tools, 2 models.

Models:
  1. planner (chat LLM, OpenAI-compatible, default openai/gpt-oss-20b via Groq)
     — mirrors video-agent/agent.py build_agent().
  2. decider (Jev System One model via jev_client.decide_cursor)
     — typed x/y + confidence per still frame, no strings to parse.

Tools (whitelisted, nothing else):
  1. read_frame  — still JPEG keyframe -> braille text -> Jev decision (jev read)
  2. move_cursor — humanized x/y move (+optional click/type/key), humanize=true Camoufox

Task loop (agent_runner): each step the planner (structured JevStep output)
receives the task instruction + braille frame + Jev grounding hint + history,
and returns the single next action. Loop ends on done, budget, or max_steps.
Secrets ({ENV_NAME} placeholders) are resolved at execution inside
move_cursor and never enter prompts or logs.

Capabilities pattern per https://pydantic.dev/docs/ai/capabilities/overview/:
Agent(..., capabilities=[...]) with exactly these two function tools.
"""

from __future__ import annotations

import os

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from pydantic import BaseModel, Field

from src.capability import CamoufoxDeps
from src.capability.camoufox_capability import CamoufoxCapability

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-20b"

INSTRUCTIONS = """\
You are the planner for a Jev cursor solver driving a headed Camoufox browser.
You have EXACTLY two tools: read_frame and move_cursor. No shell, no files, no network.

Loop: call read_frame to get a Jev x/y decision from the current still,
then call move_cursor(x, y, click, humanize=true) to act on it.
Batch 2-4 calls per response to keep the cursor alive during model round-trips.
Keep the cursor moving for the whole budget. Be terse.
"""


STEP_INSTRUCTIONS = """\
You drive a headed Camoufox browser one action at a time to complete the TASK
using the video-agent loop: open page -> find the element to act on -> circle
it -> act (type/click) -> next element -> submit.

Each turn you receive:
- TASK (+ optional NEW INSTRUCTIONS that override the TASK where they conflict)
- Jev grounding hint (x, y, confidence for the most visible actionable element)
- ELEMENT MAP: [idx] kind(type) "label/placeholder" @ x,y — visible elements,
  reading order. This is your primary targeting source.
- PAGE TEXT: the visible words on the page — read results from here
- Page braille (coarse visual texture of what the cursor is on)
- HISTORY of prior (reasoning, action) pairs

Return the single next action as a JevStep:
- reasoning: ALWAYS fill. One short sentence: what you see and why this action.
- element: idx from the ELEMENT MAP. When set, the harness scrolls that element
  into view, circles it, clicks it, then types type_text and presses key.
  For inputs use element=<field idx> + type_text; credentials use the exact
  {ENV} placeholder written in the TASK — never invent values.
- x, y: fallback pixel aim (0-1) only for regions with no map element
  (e.g. scrolling a canvas area). Copy the map's x,y when using an element.
- click: pixel-mode left click (element mode clicks automatically).
- key: Enter to submit, Tab to advance a field, Escape to dismiss a dialog.
- goto: the next URL to open (https://...) when the current page is finished —
  set it alone (no click/type on the same step); the harness navigates.
- done: true only when PAGE TEXT (or HISTORY) observably shows the TASK complete
  (e.g. signed-in landing state, a confirmation line on the page). When done,
  note MUST quote the concrete outcome (the fact found / the confirmation words
  from PAGE TEXT) — done with an empty note is rejected by the harness. A page
  still loading ("Looking for results", blank, or mid-transition) is NOT
  complete: wait one step (small move, no click) and re-observe.

For multi-part tasks (e.g. "sign in, then look something up"), finish one part on
its page, then goto the next part's page, then continue.

Pace: one field per step — fill email, next step fill password, next step
click the submit button. If the page did not advance after a submit and no
error is visible, wait one step (small move, no click) and re-observe.
"""


class JevStep(BaseModel):
    """One planner action in the task loop."""

    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(
        "", max_length=300, description="One-sentence explain of this action (streamed to operator)."
    )
    element: int | None = Field(None, ge=0, description="ELEMENT MAP idx to scroll+circle+click.")
    click: bool = False
    type_text: str | None = Field(None, max_length=500)
    key: str | None = Field(None, pattern="^(Enter|Tab|Escape)$")
    goto: str | None = Field(
        None, max_length=500, description="URL to navigate to when the current page is done with."
    )
    done: bool = False
    note: str = Field("", max_length=200)


def _make_model(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
):
    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is required (set in .env.local / env)")
    provider = OpenAIProvider(
        base_url=base_url or os.environ.get("GROQ_BASE_URL") or DEFAULT_BASE_URL,
        api_key=key,
    )
    return OpenAIChatModel(
        model_name or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL,
        provider=provider,
    )


def build_jev_agent(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> tuple[Agent[CamoufoxDeps], CamoufoxCapability]:
    model = _make_model(base_url=base_url, api_key=api_key, model_name=model_name)
    capability = JevCapability()
    agent: Agent[CamoufoxDeps] = Agent[CamoufoxDeps](
        model,
        system_prompt=INSTRUCTIONS,
        deps_type=CamoufoxDeps,
        name="open-typesafe-camoufox",
        description="Reads still frames via Jev, moves cursor humanized.",
    )
    # Whitelist: exactly 2 tools.
    agent.tool(capability.read_frame, name="read_frame", description="Sample still frame -> Jev x/y decision.")
    agent.tool(capability.move_cursor, name="move_cursor", description="Humanized x/y move (+optional click/type/key).")
    return agent, capability


def build_step_agent(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model_name: str | None = None,
) -> Agent[None]:
    """Planner that returns one typed JevStep per turn (no tools).

    Used by the task loop: instruction + braille frame + Jev hint in,
    single next action out. Structured output keeps the loop parseable.
    """
    model = _make_model(base_url=base_url, api_key=api_key, model_name=model_name)
    return Agent[None](
        model,
        system_prompt=STEP_INSTRUCTIONS,
        output_type=JevStep,
        name="open-jev-stepper",
        description="Returns the single next browser action for the task.",
    )


# Imported late to avoid circulars: JevCapability lives in src/capability/jev_actions.py
from src.capability.jev_actions import JevCapability  # noqa: E402

__all__ = ["build_jev_agent", "build_step_agent", "JevStep", "CamoufoxCapability", "CamoufoxDeps", "JevCapability"]
