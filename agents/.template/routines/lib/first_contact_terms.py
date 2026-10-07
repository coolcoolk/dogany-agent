"""DGN-1837: what a kit-pending first-contact opener must NOT say -- the single
source of the term list.

A main with no kit yet (Primary focus still the "(set at onboarding"
placeholder) opens with who it is (a personal agent, newly born -- owner
copy 2026-10-03 14:34; DGN-1848: it names no owner, so no possessive in
front of it) plus a light invitation to say hello --
nothing else, and only on the opening turn (r2: routines/lib/
first_contact_state.py records it; later turns are identity setup).  Measured
2026-10-03: the opener listed schedules / to-dos / looking things up /
talking through worries and addressed the owner as "dangsin", which reads
like the life kit is pre-installed.

Two readers, never a fork:
  - routines/onboarding-gate-stop.py checks the opener against
    opener_violations() (Stop block once, regenerate).
  - routines/onboarding-check.py renders prohibited_terms_line() into the
    injected first-contact rule, so the rule text names the same terms the
    gate enforces.

Match corpus for model output, not owner copy.  Korean entries are escaped
to keep this file ASCII (RULES Code=English/ASCII).  English "you"/"your" is
deliberately NOT policed by the gate (too common in a greeting to match
safely; the rule text forbids an owner possessive instead); the Korean
second-person pronouns and substitute possessives are the measured defects.
"""
import re

# Domain / task / capability words (Korean stems; substring match).
KO_DOMAIN_TERMS = (
    "\uc77c\uc815",              # iljeong (schedule)
    "\uc2a4\ucf00\uc904",        # seukejul (schedule)
    "\uce98\ub9b0\ub354",        # kaellindeo (calendar)
    "\ud560 \uc77c",             # hal il (to-do)
    "\ud560\uc77c",              # halil (to-do)
    "\ud22c\ub450",              # tudu (to-do)
    "\uc815\ub9ac",              # jeongni (organize)
    "\uac80\uc0c9",              # geomsaek (search)
    "\ucc3e\uc544",              # chaja (look up)
    "\uc870\uc0ac",              # josa (research)
    "\uace0\ubbfc",              # gomin (worries)
    "\uc0dd\ud65c",              # saenghwal (daily life)
    "\uc77c\uc0c1",              # ilsang (everyday)
    "\uac74\uac15",              # geongang (health)
    "\uc6b4\ub3d9",              # undong (exercise)
    "\uc2dd\ub2e8",              # sikdan (diet)
    "\ub2e4\uc774\uc5b4\ud2b8",  # daieoteu (diet)
    "\ub3c8",                    # don (money)
    "\uac00\uacc4",              # gagye (household budget)
    "\uc7ac\uc815",              # jaejeong (finance)
    "\uc9c0\ucd9c",              # jichul (spending)
    "\uc608\uc0b0",              # yesan (budget)
    "\uc5c5\ubb34",              # eommu (work)
    "\uba54\uc77c",              # meil (mail; covers imeil)
    "\uc54c\ub9bc",              # allim (notification)
    "\ub9ac\ub9c8\uc778\ub4dc",  # rimaindeu (remind)
    "\ub9ac\ub9c8\uc778\ub354",  # rimaindeo (reminder)
    "\ube0c\ub9ac\ud551",        # beuriping (briefing)
    "\uc57d\uc18d",              # yaksok (appointment)
    "\ub274\uc2a4",              # nyuseu (news)
    "\ub0a0\uc528",              # nalssi (weather)
    "\uc1fc\ud551",              # syoping (shopping)
)

# English equivalents: (display word, case-insensitive word-bounded regex).
EN_DOMAIN_TERMS = (
    ("schedule", r"schedul\w*"), ("calendar", r"calendars?"),
    ("to-do", r"to-?dos?"), ("task", r"tasks?"), ("errand", r"errands?"),
    ("organize", r"organi[sz]\w*"), ("search", r"search\w*"),
    ("look up", r"look(?:ing)?\s+(?:\w+\s+)?up"), ("research", r"research\w*"),
    ("worry", r"worr(?:y|ies)"), ("concern", r"concerns?"), ("life", r"life"),
    ("health", r"health\w*"), ("exercise", r"exercis\w*"),
    ("workout", r"workouts?"), ("fitness", r"fitness"), ("diet", r"diet\w*"),
    ("meal", r"meals?"), ("money", r"money"), ("budget", r"budget\w*"),
    ("finance", r"financ\w*"), ("expense", r"expenses?"),
    ("spending", r"spending"), ("email", r"e-?mails?"), ("mail", r"mail"),
    ("inbox", r"inbox"), ("reminder", r"remind\w*"),
    ("notification", r"notifications?"), ("alert", r"alerts?"),
    ("briefing", r"briefings?"), ("appointment", r"appointments?"),
    ("news", r"news"), ("weather", r"weather"), ("shopping", r"shopping"),
)
_EN_DOMAIN_RE = re.compile(
    r"\b(?:%s)\b" % "|".join("(?P<t%d>%s)" % (i, rx)
                             for i, (_w, rx) in enumerate(EN_DOMAIN_TERMS)),
    re.IGNORECASE)

# Korean second-person pronouns.  Bare "neo" / "geudae" stems also occur
# inside common words ("too", "as is"), so those count only with a particle.
KO_SECOND_PERSON = (
    "\ub2f9\uc2e0",              # dangsin
    "\uadf8\ub300\ub294", "\uadf8\ub300\uc758", "\uadf8\ub300\uac00",  # geudae + particle
    "\ub108\ub294", "\ub108\uc758", "\ub108\ub97c", "\ub108\ud55c\ud14c",
    "\ub108\uc5d0\uac8c", "\ub108\ub791",
    "\ub124\uac00",              # nega
    "\uc790\ub124",              # jane
)


# DGN-1848 (rehearsal 2026-10-04 10:09): with "your" banned, the model put
# a substitute possessive in front of "personal agent" ("i gos-ui", "this
# place's").  The opener names no owner, so these count as addressing the
# user.  Possessive forms only (with the -ui particle): the bare place words
# also occur in ordinary sentences.
KO_SUBSTITUTE_POSSESSIVE = (
    "\uc774 \uacf3\uc758",        # i gos-ui (this place's)
    "\uc774\uacf3\uc758",         # igos-ui (this place's)
    "\uc5ec\uae30\uc758",         # yeogi-ui (here's)
)


def opener_violations(text, address_terms=()):
    """{"terms": [...], "pronouns": [...]} found in a kit-pending opener, or
    None when clean.  address_terms: owner-name address forms (name
    guesses) the caller resolved; they count as pronouns here, as do the
    substitute possessives (DGN-1848)."""
    body = text or ""
    terms = [t for t in KO_DOMAIN_TERMS if t in body]
    for m in _EN_DOMAIN_RE.finditer(body):
        word = EN_DOMAIN_TERMS[int(m.lastgroup[1:])][0]
        if word not in terms:
            terms.append(word)
    pronouns = [p for p in KO_SECOND_PERSON if p in body]
    pronouns += [p for p in KO_SUBSTITUTE_POSSESSIVE if p in body]
    pronouns += [a for a in address_terms if a and a in body]
    if not terms and not pronouns:
        return None
    return {"terms": terms, "pronouns": pronouns}


def prohibited_terms_line(lang):
    """The term list as rule text for the injected first-contact rule."""
    if (lang or "").lower().startswith("ko"):
        return ("\uae08\uc9c0 \ub2e8\uc5b4(\ud558\ub098\ub3c4 \uc4f0\uc9c0 "
                "\ub9c8\uc138\uc694): " + ", ".join(KO_DOMAIN_TERMS)
                + "; \uc778\uce6d \ub300\uba85\uc0ac: "
                + ", ".join(KO_SECOND_PERSON)
                + "; \uc18c\uc720\uaca9: "
                + ", ".join(KO_SUBSTITUTE_POSSESSIVE) + ".")
    return ("Forbidden words (use none): "
            + ", ".join(w for w, _rx in EN_DOMAIN_TERMS)
            + "; and no second-person pronoun, possessive or name guess for "
            "the user -- not even 'your personal agent' -- and no substitute "
            "possessive or place word such as 'this place's' or 'here's'.")
