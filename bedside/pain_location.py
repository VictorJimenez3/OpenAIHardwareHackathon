"""Small, deterministic yes/no tree for optional pain-location detail.

Three yes/no partitions lead to a broad region. An affirmative answer to a
region question identifies that region. The alternative region inferred from
a No answer still needs explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocationPrompt:
    """One binary partition in the location tree."""

    text: str
    yes_stage: str | None
    no_stage: str | None
    yes_location: str | None = None
    no_location: str | None = None


FIRST_STAGE = "location_upper_lower"
CONFIRM_STAGE = "location_confirm"


# The first question splits eight regions into two groups of four. The second
# splits each group into pairs, and the third asks about one region in the pair.
PROMPTS: dict[str, LocationPrompt] = {
    FIRST_STAGE: LocationPrompt(
        "Is the pain in your upper body?",
        yes_stage="location_upper_group",
        no_stage="location_lower_group",
    ),
    "location_upper_group": LocationPrompt(
        "Pain in your head, neck, arm, or shoulder?",
        yes_stage="location_upper_head",
        no_stage="location_upper_chest",
    ),
    "location_upper_head": LocationPrompt(
        "Pain in your head or neck?",
        yes_stage="review",
        no_stage=CONFIRM_STAGE,
        yes_location="head or neck",
        no_location="arm or shoulder",
    ),
    "location_upper_chest": LocationPrompt(
        "Pain in your chest or upper back?",
        yes_stage="review",
        no_stage=CONFIRM_STAGE,
        yes_location="chest or upper back",
        no_location="abdomen",
    ),
    "location_lower_group": LocationPrompt(
        "Pain in your hip, pelvis, or thigh?",
        yes_stage="location_lower_hip",
        no_stage="location_lower_leg",
    ),
    "location_lower_hip": LocationPrompt(
        "Pain in your hip or pelvis?",
        yes_stage="review",
        no_stage=CONFIRM_STAGE,
        yes_location="hip or pelvis",
        no_location="thigh",
    ),
    "location_lower_leg": LocationPrompt(
        "Pain in your knee or lower leg?",
        yes_stage="review",
        no_stage=CONFIRM_STAGE,
        yes_location="knee or lower leg",
        no_location="ankle or foot",
    ),
}

KNOWN_LOCATIONS = {
    location
    for prompt in PROMPTS.values()
    for location in (prompt.yes_location, prompt.no_location)
    if location is not None
}


def prompt_for(stage: str) -> LocationPrompt | None:
    """Return the approved partition for ``stage``."""

    return PROMPTS.get(stage)


def next_location_stage(stage: str, answer: str) -> tuple[str | None, str | None]:
    """Advance one partition and return ``(stage, candidate_location)``.

    Only the final partition returns a candidate. Its Yes branch proceeds to
    review, and its No branch asks for confirmation. Invalid answers leave the
    question open.
    """

    prompt = PROMPTS.get(stage)
    if prompt is None or answer not in {"yes", "no"}:
        return None, None
    if answer == "yes":
        return prompt.yes_stage, prompt.yes_location
    return prompt.no_stage, prompt.no_location


def confirmation_question(location: str) -> str:
    """Build the final explicit confirmation from a fixed approved label."""

    if location not in KNOWN_LOCATIONS:
        raise ValueError("unsupported location")
    return f"Is the pain in your {location}?"


def update_message(location: str) -> str:
    """Build the exact first-person location update."""

    # Reuse the same allow-list as the confirmation builder.  This is a
    # defensive boundary in case a future caller bypasses the tree.
    confirmation_question(location)
    return f"I have pain in my {location}."
