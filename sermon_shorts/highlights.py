"""Highlight selection via the Claude API with structured output.

Sends the timestamped transcript of the service and asks for the most
shareable sermon moments. Uses `client.messages.parse()` so the response
is a validated Pydantic object — no JSON wrangling.
"""

from __future__ import annotations

import sys

from pydantic import BaseModel

MODEL = "claude-opus-4-8"


class Clip(BaseModel):
    title: str          # short social-media-ready title
    hook: str           # the opening line of the clip, verbatim from the transcript
    start: float        # seconds from the beginning of the video
    end: float          # seconds from the beginning of the video
    score: int          # 0-100 shareability
    reason: str         # one sentence on why this moment works as a standalone clip
    description: str    # ready-to-paste description for YouTube Shorts / Reels


class ClipSelection(BaseModel):
    service_summary: str
    clips: list[Clip]


SYSTEM_PROMPT = """You select short-form video clips from church service recordings.

You will receive a timestamped transcript of a full service. Choose the moments most \
worth publishing as standalone vertical clips (Instagram Reels / YouTube Shorts / TikTok).

What makes a great church clip:
- A complete, self-contained thought from the sermon: a powerful one-liner, a vivid \
illustration or story, a practical application of scripture, an encouraging word, or a \
moment of humor that lands without context.
- It starts at the natural beginning of a sentence or story and ends on a resolved thought.
- Someone scrolling who has never attended this church would stop, watch, and understand it.

Strictly avoid:
- Worship music / singing (music licensing such as CCLI generally does NOT cover social \
media, so song segments must never be clipped).
- Announcements, offering/giving segments, logistics, greetings, and transitions.
- Scripture readings with no commentary, and prayers unless the moment is exceptionally \
powerful and self-contained.

Rules:
- Each clip must be 20-75 seconds long.
- Clips must not overlap.
- Use the transcript's timestamps; start/end must land on sentence boundaries.
- Score each clip 0-100 for shareability.
- The `hook` field must quote the clip's opening words verbatim so timing can be verified.
- The `description` field is a ready-to-paste video description: 1-3 warm, inviting \
sentences that front-load searchable keywords (the topic, scripture reference if one \
is named, key phrases someone might search), followed by 4-6 relevant hashtags on a \
final line (e.g. #sermon #faith #bible). Write it for a viewer, not a churchgoer — no \
insider jargon, and don't invent a church name, speaker name, or scripture reference \
that isn't in the transcript. Use plain keyboard punctuation only (hyphens and straight \
quotes - no em dashes or smart quotes), since descriptions get copy-pasted into upload \
forms."""


def select_highlights(transcript_text: str, n_clips: int) -> ClipSelection:
    import anthropic

    # The SDK resolves credentials from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
    # or an `ant auth login` profile — so attempt the call and only explain
    # setup if authentication actually fails.
    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as e:
        sys.exit(_auth_help(str(e)))

    try:
        response = _request(client, transcript_text, n_clips)
    except anthropic.AuthenticationError as e:
        sys.exit(_auth_help(str(e)))

    selection = response.parsed_output
    if selection is None:
        sys.exit("Claude's response could not be parsed into the clip schema. Try again.")

    for c in selection.clips:
        c.description = plain_punctuation(c.description)

    # Keep only sane clips, best first
    valid = [c for c in selection.clips if c.end > c.start and 10 <= (c.end - c.start) <= 120]
    valid.sort(key=lambda c: c.score, reverse=True)
    selection.clips = valid[:n_clips]
    return selection


_TYPOGRAPHIC = {
    "—": " - ",  # em dash
    "–": "-",    # en dash
    "‘": "'", "’": "'",      # curly single quotes
    "“": '"', "”": '"',      # curly double quotes
    "…": "...",  # ellipsis
    " ": " ",    # non-breaking space
}


def plain_punctuation(text: str) -> str:
    """Normalize typographic characters so descriptions survive copy-paste anywhere."""
    import re

    for bad, good in _TYPOGRAPHIC.items():
        text = text.replace(bad, good)
    text = re.sub(r" {2,}", " ", text)
    return "\n".join(line.strip() for line in text.splitlines())


def _auth_help(detail: str) -> str:
    return (
        f"Could not authenticate with the Claude API ({detail}).\n"
        "Put your key in a .env file in the sermon-shorts folder:\n"
        "  ANTHROPIC_API_KEY=sk-ant-...\n"
        "(copy .env.example to .env), or set the ANTHROPIC_API_KEY environment variable.\n"
        "Get a key at https://platform.claude.com/"
    )


def _request(client, transcript_text: str, n_clips: int):
    return client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Select the {n_clips} best clips from this service transcript. "
                f"Format: each line is [start_seconds-end_seconds] text.\n\n"
                f"{transcript_text}"
            ),
        }],
        output_format=ClipSelection,
    )
