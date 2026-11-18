"""
Topic tags shown as pills on cards, with a "show only" filter on the pages.

Claude assigns them automatically: the crawler re-decides every entry's tags
each week from its live page, and the scanner tags new finds. To add a topic
(e.g. AI security), add an entry here and the matching CSS/button in the
two HTML files.
"""

TOPICS = {
    "biosecurity": {
        "label": "Biosecurity",
        "rule": (
            "include it if the opportunity is primarily about biosecurity, biodefense, "
            "pandemic prevention or AI-enabled biological risk, OR has a dedicated, "
            "named biosecurity track, team, stream or funding area. A passing "
            "mention of biology in a list of topics does NOT count."
        ),
    },
    "governance": {
        "label": "Policy & Governance",
        "rule": (
            "include it if the opportunity is primarily about AI governance, AI policy, "
            "AI law, standards or regulation, OR has a dedicated, named governance or "
            "policy track, team, stream or funding area. Technical research or "
            "evaluation organisations whose results are used by policymakers do NOT count."
        ),
    },
    "security": {
        "label": "Security",
        "rule": (
            "include it if the opportunity is primarily about AI security or "
            "cybersecurity as it relates to AI — securing model weights, compute and AI "
            "infrastructure, AI-enabled cyber threats, adversarial attacks and security "
            "red-teaming of AI systems — OR has a dedicated, named security track, team, "
            "stream or funding area. Generic IT or 'safe and secure AI' slogans do NOT count."
        ),
    },
}


# Applies to every topic. Tags exist so a reader in that specialty can filter
# to things meant for THEM; over-tagging makes the filter useless.
GENERAL_RULE = (
    "Be STRICT: most entries get no tags. Tag only when someone in that specialty "
    "would see this as specifically for them. Do NOT tag broad job boards, "
    "aggregators, reading/self-study lists, general AI-safety fellowships, labs or "
    "generalist funders just because the topic is one of several areas they "
    "mention — only if they run a dedicated, named program, track, team or "
    "funding area for it."
)


def prompt_block():
    """Lines describing each topic, for inclusion in a Claude prompt."""
    return GENERAL_RULE + "\n" + "\n".join(
        f'  - "{k}": {v["rule"]}' for k, v in TOPICS.items()
    )


def effective(resource, auto_tags=None):
    """
    Final tags = Claude's tags, plus/minus the entry's manual "tag_overrides"
    ({"add": [...], "remove": [...]}), which always win. Use overrides for facts
    the page can't show (e.g. MATS's bio track before its page mentions it).
    """
    tags = list(resource.get("tags") or []) if auto_tags is None else list(auto_tags)
    ov = resource.get("tag_overrides") or {}
    tags = [t for t in tags if t not in (ov.get("remove") or [])]
    tags += [t for t in (ov.get("add") or []) if t not in tags]
    return [k for k in TOPICS if k in tags]


def clean(raw):
    """Keep only known topic keys from Claude's answer, in a stable order."""
    if not isinstance(raw, list):
        return []
    return [k for k in TOPICS if k in raw]
