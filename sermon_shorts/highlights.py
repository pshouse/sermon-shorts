"""Highlight selection via the Claude API with structured output.

Sends the timestamped transcript of the service and asks for the most
shareable sermon moments. Uses `client.messages.parse()` so the response
is a validated Pydantic object — no JSON wrangling.
"""

from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel

# Fable over Opus: on the 2026-10-11 service, given the same prompts, Opus 4.8
# kept clips opening on "Because..." / "So this is the explanation", while
# Fable 5.1 picked ideas a stranger could follow from the first sentence.
# About $0.60 a run for the two calls, versus ~$0.10.
MODEL = "claude-fable-5-1"
EFFORT = "high"


class Clip(BaseModel):
    title: str          # short social-media-ready title
    hook: str           # the opening line of the clip, verbatim from the transcript
    ending: str = ""    # the clip's final words, verbatim, where the thought completes
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

The transcript comes from speech recognition. Its lines break where the speaker paused, \
not where sentences end, and some transcripts arrive with little or no punctuation. Never \
treat the end of a line as the end of a sentence: read the words themselves and decide \
where the thought is actually complete.

What makes a great church clip: one whole idea, complete in itself. Read the sermon as \
a sequence of ideas and pick ideas, not passages. A whole idea has three parts:
1. Setup: it states the point, asks the question, or begins the story, in words a \
stranger understands with no prior context.
2. Development: the explanation, illustration, or story that carries the point.
3. Landing: the conclusion, the application, or the punchline, after which the speaker \
moves on to something new.
Good ideas to clip: a vivid illustration or story told start to finish, a practical \
application of scripture, an encouraging word, a powerful line together with the setup \
that makes it land, or humor that works without context.

Test every candidate as someone scrolling who has never heard this sermon:
- The first sentence must make sense on its own. Openings that lean on what came before \
fail: "But...", "And...", "So...", "That's why...", "this guy", "he", "they", "that \
passage" when the clip never says who or what. Start earlier to include the setup, or \
choose another idea.
- The last sentence must land the idea. If the next sentences still belong to the same \
point (they finish the example, answer the question, or state the application), the \
clip is not done: include them.
- If a stranger would ask "wait, who?" or "so what?", it is not a clip.

Strictly avoid:
- Worship music / singing (music licensing such as CCLI generally does NOT cover social \
media, so song segments must never be clipped).
- Announcements, offering/giving segments, logistics, greetings, and transitions.
- Scripture readings with no commentary, and prayers unless the moment is exceptionally \
powerful and self-contained.

Rules:
- Return twice as many candidates as you are asked for, best first. Each is checked \
again before use, so prefer complete ideas over polished but partial ones.
- Aim for 45-75 seconds: long enough to set up a point and land it. Running to 90 \
seconds is fine when that is what it takes to finish the thought; go under 30 seconds \
only for a one-liner that truly stands alone. Cutting a thought short to fit is never acceptable: if the resolution \
won't fit, end earlier on a line that already stands on its own, or pick another moment.
- Clips must not overlap.
- Use the transcript's timestamps. `end` is the moment the final sentence of the thought \
finishes, and the words right after `end` must begin something new. If they continue the \
sentence (a line ending on "because", "that", "we want to be"...), you have cut too early.
- Score each clip 0-100 for shareability.
- The `hook` field must quote the clip's opening words verbatim, and the `ending` field \
the last 6-10 words of the clip verbatim, ending on the word where the thought is complete. \
The clip is cut on these exact words, so they decide where it starts and stops: the hook \
must open a sentence and the ending must close one, wherever the transcript's lines break.
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
    # Streamed so a long think isn't capped by the non-streaming size limit;
    # the server-side fallback re-runs a (rare) safety refusal on another
    # model instead of failing the weekly run.
    try:
        client = anthropic.Anthropic()
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            output_config={"effort": EFFORT},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_format=output_format,
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        sys.exit(_auth_help(str(e)))
    except anthropic.AnthropicError as e:
        sys.exit(f"Claude API request failed: {e}")

    if response.stop_reason == "refusal":
        sys.exit(f"Claude declined the request ({response.stop_details}). Try again, "
                 "or run with --from-manifest if clips already exist.")
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


MIN_CLIP_SECONDS = 30    # shorter "clips" were the clearest sign of a partial idea
MIN_STANDALONE = 70      # review score below which a candidate is dropped
_REVIEW_BEFORE = 60.0    # seconds of context shown ahead of a candidate
_REVIEW_AFTER = 45.0     # ...and after it


class ClipReview(BaseModel):
    candidate: int      # the candidate's number as given
    verdict: Literal["keep", "adjust", "reject"]
    standalone: int     # 0-100: how fully a stranger follows the idea, start to finish
    start: float        # seconds — unchanged unless adjusted
    end: float          # seconds — unchanged unless adjusted
    hook: str           # opening words verbatim (revised if adjusted)
    ending: str         # closing words verbatim (revised if adjusted)
    note: str           # one sentence: what was wrong, or why it stands


class ReviewResult(BaseModel):
    reviews: list[ClipReview]


REVIEW_PROMPT = """You review candidate short-form clips cut from a church sermon, before \
they are published to people who have never heard this sermon.

Each candidate is shown exactly as it will be cut (CLIP), with the words spoken just \
before (BEFORE) and after (AFTER). The text is speech recognition: punctuation is \
unreliable, so judge by meaning, not by periods.

For each candidate, ask one question: would a stranger follow this as one whole idea, \
from its first word to its last?
- Opening: does the first sentence make sense on its own? If it leans on BEFORE ("But", \
"So", "this guy", "he", "that passage" with no name), move the start back to where the \
setup begins, if that is in BEFORE.
- Landing: does the idea finish? If the start of AFTER still belongs to the same point \
(finishes the example, answers the question, states the application), move the end \
forward to where the point lands, if that is in AFTER.
- If no adjustment within the shown text makes it whole, reject it.

Verdicts: "keep" (whole as cut), "adjust" (whole after moving the start and/or end: give \
the new start/end in seconds from the timestamps shown and the new opening/closing words), \
or "reject". `hook` is the clip's first words and `ending` its last 6-10 words, both \
quoted verbatim from the text shown. Score `standalone` 0-100 for the clip as it will be \
after your adjustment. Clips should run 45-90 seconds; under 30 is a rejection unless it \
is a single complete line that truly stands alone."""


def _timed_lines(words: list[dict]) -> str:
    """Words as lines of roughly ten, each prefixed with its start time."""
    lines = []
    for i in range(0, len(words), 10):
        chunk = words[i:i + 10]
        lines.append(f"[{chunk[0]['start']:.1f}] " + " ".join(w["word"].strip() for w in chunk))
    return "\n".join(lines)


def _candidate_bounds(transcript: dict, clip: Clip) -> tuple[float, float]:
    """Where the clip will actually be cut: on its quoted words when found."""
    from .transcribe import locate_quote, snap_to_sentences

    start, end = snap_to_sentences(transcript, clip.start, clip.end)
    hook_at = locate_quote(transcript, clip.hook, clip.start)
    end_at = locate_quote(transcript, clip.ending, clip.end, closing=True)
    return (hook_at if hook_at is not None else start,
            end_at if end_at is not None and end_at > (hook_at or start) else end)


def review_clips(transcript: dict, candidates: list[Clip]) -> list[Clip]:
    """Second pass: a stranger's-eye check of each candidate as it will be cut.

    Picking clips from a whole transcript, Claude reliably finds good
    *moments* but often trims them to a partial idea — on the 2026-10-11
    service two of three clips opened on "But..." and one stopped before the
    speaker finished his point. Shown one candidate at a time with the words
    around it, the same model is far better at judging (and repairing) where
    the idea really starts and lands.
    """
    from .transcribe import _all_words

    words = _all_words(transcript)
    blocks = []
    for n, clip in enumerate(candidates, 1):
        start, end = _candidate_bounds(transcript, clip)
        before = [w for w in words if start - _REVIEW_BEFORE <= w["start"] < start - 0.05]
        inside = [w for w in words if start - 0.05 <= w["start"] < end]
        after = [w for w in words if end <= w["start"] < end + _REVIEW_AFTER]
        blocks.append(
            f"=== Candidate {n}: \"{clip.title}\" ({start:.1f}s-{end:.1f}s, {end - start:.0f}s)\n"
            f"BEFORE:\n{_timed_lines(before)}\n\nCLIP:\n{_timed_lines(inside)}\n\n"
            f"AFTER:\n{_timed_lines(after)}\n")
    result = _parse_call(REVIEW_PROMPT, "\n".join(blocks), ReviewResult)

    reviewed = []
    for r in result.reviews:
        if not 1 <= r.candidate <= len(candidates):
            continue
        clip = candidates[r.candidate - 1]
        print(f"  review {r.candidate}: {r.verdict}, standalone {r.standalone} — {r.note}")
        if r.verdict == "reject" or r.standalone < MIN_STANDALONE:
            continue
        if r.verdict == "adjust":
            clip = clip.model_copy(update={"start": r.start, "end": r.end,
                                           "hook": r.hook, "ending": r.ending})
        start, end = _candidate_bounds(transcript, clip)
        if end - start < MIN_CLIP_SECONDS:
            print(f"    dropped: {end - start:.0f}s is too short to hold a whole idea")
            continue
        reviewed.append((r.standalone, clip))
    reviewed.sort(key=lambda sc: (sc[0], sc[1].score), reverse=True)
    return [clip for _, clip in reviewed]


def select_highlights(transcript: dict, n_clips: int,
                      church: dict | None = None) -> ClipSelection:
    from .transcribe import transcript_as_text

    selection = _parse_call(
        SYSTEM_PROMPT + _church_context(church),
        f"Select {n_clips * 2} candidate clips (twice the {n_clips} needed) from this "
        f"service transcript. Format: each line is [start_seconds-end_seconds] text."
        f"\n\n{transcript_as_text(transcript)}",
        ClipSelection,
    )

    for c in selection.clips:
        c.description = _append_footer(plain_punctuation(c.description), church)

    candidates = [c for c in selection.clips if c.end > c.start and 10 <= (c.end - c.start) <= 120]
    candidates.sort(key=lambda c: c.score, reverse=True)
    print(f"  {len(candidates)} candidates; reviewing each as a stranger would see it")
    kept = review_clips(transcript, candidates)
    if not kept:
        print("  warning: no candidate passed review — using the best unreviewed ones")
        kept = candidates
    selection.clips = kept[:n_clips]
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
