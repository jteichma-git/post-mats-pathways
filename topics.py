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
        "label": "Governance",
        "rule": (
            "include it if the opportunity is primarily about AI governance, AI policy, "
            "AI law, standards or regulation, OR has a dedicated, named governance or "
            "policy track, team, stream or funding area. Technical AI safety work that "
            "merely informs policy does NOT count."
        ),
    },
}


def prompt_block():
    """Lines describing each topic, for inclusion in a Claude prompt."""
    return "\n".join(f'  - "{k}": {v["rule"]}' for k, v in TOPICS.items())


def clean(raw):
    """Keep only known topic keys from Claude's answer, in a stable order."""
    if not isinstance(raw, list):
        return []
    return [k for k in TOPICS if k in raw]
