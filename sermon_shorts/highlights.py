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


class SermonWindow(BaseModel):
    title: str          # short descriptive sermon title
    start: float        # seconds — where the sermon begins
    end: float          # seconds — where the sermon ends
    reason: str         # one sentence on how the boundaries were identified


SERMON_SYSTEM_PROMPT = """You identify the sermon within a full church service transcript.

You will receive a timestamped transcript of an entire service: worship, announcements, \
readings, the sermon, prayers, communion, closing. Find the main sermon (the message):

- It begins when the preacher starts the message — include their opening remarks, \
scripture introduction, or opening prayer when those flow directly into the message.
- It ends when the message concludes — typically at the preacher's closing prayer or \
final application. Exclude the worship set, announcements, offering, communion \
liturgy, and anything after the message ends.
- Use the transcript's timestamps and land on sentence boundaries.
- Provide a short descriptive title for the sermon, and one sentence explaining how \
you identified the boundaries."""


def _parse_call(system: str, user_content: str, output_format):
    """Shared Claude structured-output request with friendly auth errors."""
    import anthropic

    # The SDK resolves credentials from ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
    # or an `ant auth login` profile — so attempt the call and only explain
    # setup if authentication actually fails.
    try:
        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_format=output_format,
        )
    except anthropic.AuthenticationError as e:
        sys.exit(_auth_help(str(e)))
    except anthropic.AnthropicError as e:
        sys.exit(f"Claude API request failed: {e}")

    if response.parsed_output is None:
        sys.exit("Claude's response could not be parsed into the expected schema. Try again.")
    return response.parsed_output


def find_sermon(transcript_text: str) -> SermonWindow:
    """Locate the sermon within a full-service transcript."""
    window = _parse_call(
        SERMON_SYSTEM_PROMPT,
        f"Find the sermon in this service transcript. "
        f"Format: each line is [start_seconds-end_seconds] text.\n\n{transcript_text}",
        SermonWindow,
    )
    if window.end <= window.start:
        sys.exit(f"Claude returned an invalid sermon window ({window.start}-{window.end}).")
    return window


def _church_context(church: dict | None) -> str:
    """Extra system-prompt lines when a church profile is configured."""
    if not church:
        return ""
    lines = []
    if church.get("church_name"):
        lines.append(f"The church is {church['church_name']}.")
    if church.get("speaker"):
        lines.append(f"The speaker is {church['speaker']}.")
    if not lines:
        return ""
    return (
        "\n\nChurch profile (verified, safe to use): "
        + " ".join(lines)
        + " You may mention these naturally in titles and descriptions."
    )


def _append_footer(description: str, church: dict | None) -> str:
    """Insert the configured footer line before the hashtag line."""
    footer = (church or {}).get("footer", "").strip()
    if not footer:
        return description
    lines = description.splitlines()
    if lines and lines[-1].lstrip().startswith("#"):
        lines.insert(len(lines) - 1, footer)
    else:
        lines.append(footer)
    return "\n".join(lines)


def select_highlights(transcript_text: str, n_clips: int,
                      church: dict | None = None) -> ClipSelection:
    selection = _parse_call(
        SYSTEM_PROMPT + _church_context(church),
        f"Select the {n_clips} best clips from this service transcript. "
        f"Format: each line is [start_seconds-end_seconds] text.\n\n{transcript_text}",
        ClipSelection,
    )

    for c in selection.clips:
        c.description = _append_footer(plain_punctuation(c.description), church)

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
