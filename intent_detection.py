"""Layered intent detection (keyword + embedding) and per-turn prompt injection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import numpy as np

from agent_config import format_slots_compact

if TYPE_CHECKING:
    from agent_config import ConversationSession

INTENT_PATTERNS: dict[str, str] = {
    "booking_intent": (
        r"\b(book|schedule|meeting|appointment|slot|calendar|available|availability|"
        r"when can|what time|set up a call|fix a time|arrange|block|demo|connect|"
        r"catch up|hop on|jump on)\b"
    ),
    "pricing_intent": (
        r"\b(price|pricing|cost|how much|charges|rate|rates|fee|fees|expensive|"
        r"cheap|affordable|budget|quote|quotation|invoice|payment|pay|per piece|"
        r"per unit|bulk rate|wholesale|MOQ|minimum order)\b"
    ),
    "product_intent": (
        r"\b(what do you (have|sell|offer)|tell me more|more details|product|products|"
        r"catalogue|catalog|range|collection|casserole|lunchbox|water bottle|"
        r"container|jar|tray|dinnerware|storage|specifications|spec|quality|"
        r"material|plastic|BPA|food grade|size|capacity|colour|color|variant|"
        r"sample|brochure|what all|what else)\b"
    ),
    "objection_busy": (
        r"\b(busy|not a good time|bad time|can't talk|cannot talk|in a meeting|"
        r"occupied|tied up|call (me )?back|call later|later|another time|"
        r"right now (is)? not|not right now|catch me later|call tomorrow|"
        r"give me a (min|minute|second)|bit tied)\b"
    ),
    "objection_no_budget": (
        r"\b(no budget|tight budget|budget (is )?low|can't afford|cannot afford|"
        r"not in budget|financially|money (is )?tight|no funds|limited budget|"
        r"saving|cut(ting)? cost|cost cutting|investment (is )?high|too expensive|"
        r"out of our budget|not allocated|no sanctioned)\b"
    ),
    "objection_send_info": (
        r"\b(send (me )?(an )?email|send (on )?whatsapp|send (the )?details|"
        r"send (a )?brochure|send (the )?catalogue|mail (it|me)|drop (me )?a mail|"
        r"share (the )?info|send (the )?link|share (on )?email|send (it )?across|"
        r"email (it|me)|whatsapp me|send (me )?something)\b"
    ),
    "objection_not_interested": (
        r"\b(not interested|don't need|do not need|no (need|thanks|thank you)|"
        r"not (looking|required|needed)|we('re| are) (good|fine|set|sorted)|"
        r"don't (want|require)|no requirement|pass|not for (me|us)|"
        r"doesn't (suit|fit|apply)|not relevant|not applicable)\b"
    ),
    "objection_existing_vendor": (
        r"\b(already (have|got|using|working with)|existing (supplier|vendor|partner)|"
        r"current supplier|current vendor|already (sourcing|buying|purchasing)|"
        r"have a (supplier|vendor|source)|tied (up )?with|contract with|"
        r"someone else|another supplier|already sorted)\b"
    ),
    "opt_out": (
        r"\b(remove (me|my number)|don't call( me)?|do not call|stop calling|"
        r"stop contacting|never call|take (me )?off|unsubscribe|DNC|"
        r"block (this )?number|how did you get (my number|this number)|"
        r"don't contact|no more calls|this is spam|report (you|this))\b"
    ),
    "human_request": (
        r"\b(talk to (a |someone |a )?(human|person|real person|agent|representative|rep)|"
        r"speak to (a |someone |a )?(human|person|real person)|actual person|"
        r"not (a |an )?bot|are you (a |an )?(bot|robot|AI|machine)|"
        r"transfer (me|this call)|get me (your )?manager|supervisor|"
        r"connect me to|put me through to|human please)\b"
    ),
    "anger_escalation": (
        r"\b(stop (wasting|bothering)|waste (of )?time|ridiculous|nonsense|"
        r"irritating|annoying|harassment|harassing|how dare|don't bother|"
        r"bloody|pathetic|rubbish|nonsense|get lost|go away|"
        r"threatening|legal action|consumer forum|complaint)\b"
    ),
    "positive_signal": (
        r"\b(interested|sounds (good|great|nice)|that('s| is) (good|great|interesting|nice)|"
        r"tell me more|go ahead|yes please|I('d| would) like|love to|"
        r"good (idea|option|point)|makes sense|sure (go ahead|why not)|"
        r"I'm (listening|open)|what else|keep going|continue)\b"
    ),
    "callback_request": (
        r"\b(call (me )?(back|later|tomorrow|after)|call (me )?at [0-9]|"
        r"better time (is|would be)|reach (me )?at|try (me )?(again )?(at|after|later)|"
        r"ping me|available (at|after|by)|free (at|after)|"
        r"in (an? )?(hour|while|bit)|after [0-9])\b"
    ),
    "order_quantity_intent": (
        r"\b(how many|minimum (order|quantity)|MOQ|bulk|wholesale|"
        r"large (order|quantity)|order (size|volume)|pieces|units|"
        r"dozen|gross|pallet|carton|can (I|we) order|"
        r"small (order|quantity)|trial order|sample order)\b"
    ),
    "logistics_intent": (
        r"\b(deliver(y)?|shipping|ship|dispatch|freight|courier|"
        r"how long (to|does it)|lead time|when (will|can) (I|we) (get|receive)|"
        r"reach (me|us)|transit|logistics|warehouse|local (pick ?up|collection)|"
        r"pan india|all over india|serviceable)\b"
    ),
    "confirmation_signal": (
        r"\b(confirm|confirmed|yes (that('s| is) (fine|good|correct)|I'll (take|go with))|"
        r"that works|works for me|lock (it |that )?in|book (it|that)|"
        r"go ahead (and )?book|see you (then|there)|noted)\b"
    ),
}

INTENT_EMBEDDING_ANCHORS: dict[str, list[str]] = {
    "booking_intent": [
        "I want to schedule a meeting with you",
        "can we book a time to talk",
        "let's set up a call",
        "when are you free to connect",
        "I'd like to see a demo",
        "can we hop on a call sometime",
    ],
    "pricing_intent": [
        "what will it cost me",
        "how much do I have to pay",
        "give me your best price",
        "what's the damage",
        "is it value for money",
        "do you have any offers or discounts",
        "what are your bulk rates",
    ],
    "product_intent": [
        "tell me more about what you sell",
        "what exactly are you offering",
        "I want to know about your product range",
        "what materials are these made from",
        "do you have food grade products",
        "what sizes and variants are available",
        "can I see a catalogue",
    ],
    "objection_busy": [
        "this isn't a great time for me",
        "I'm in the middle of something",
        "can we talk some other time",
        "I'll call you back when I'm free",
        "catch me after lunch",
        "reach out to me tomorrow",
    ],
    "objection_no_budget": [
        "we're watching our spending right now",
        "money is tight this quarter",
        "we don't have anything allocated for this",
        "it's not in our plans financially",
        "we've already spent our budget",
        "I can't justify this expense right now",
    ],
    "objection_send_info": [
        "just send me the details over email",
        "drop me a message on WhatsApp",
        "can you share a brochure",
        "send me something I can look at",
        "I prefer to read first before deciding",
        "mail me the catalogue and I'll get back",
    ],
    "objection_not_interested": [
        "I don't think this is for us",
        "we're happy with how things are",
        "this doesn't really apply to our situation",
        "we're not looking for anything like this",
        "I don't see a need for this in my canteen",
        "thanks but we're all sorted",
    ],
    "objection_existing_vendor": [
        "we already get this from somewhere else",
        "we have a supplier we're happy with",
        "I've been buying from the same place for years",
        "we're under a contract with our current vendor",
        "I don't want to switch right now",
        "our current source is working fine",
    ],
    "opt_out": [
        "please remove my number from your list",
        "I don't want any more calls from you",
        "how did you get this number",
        "stop reaching out to me",
        "I'm going to report this as spam",
        "don't ever call this number again",
    ],
    "human_request": [
        "I want to speak with a real person",
        "can I talk to someone from your team",
        "are you actually a bot",
        "I don't want to talk to a machine",
        "put me through to your manager",
        "get me a human being please",
    ],
    "anger_escalation": [
        "you're wasting my time",
        "this is extremely frustrating",
        "I've told you people not to call",
        "I'm going to file a complaint",
        "this is borderline harassment",
        "I'm very angry right now",
    ],
    "positive_signal": [
        "that actually sounds quite useful",
        "I could see this working for my canteen",
        "okay you've got my attention",
        "that's interesting, tell me more",
        "this might be something we need",
        "I like what I'm hearing",
    ],
    "callback_request": [
        "call me back in an hour",
        "try me again this evening",
        "I'm free after 5 PM",
        "reach out tomorrow morning",
        "give me a call next week",
        "I'll be available after lunch",
    ],
    "order_quantity_intent": [
        "what's the minimum I can order",
        "do you do small trial orders",
        "I need a bulk quantity for my canteen",
        "can I get a sample batch first",
        "how many pieces per carton",
        "do you have wholesale pricing for large orders",
    ],
    "logistics_intent": [
        "can you deliver to my location",
        "how long does shipping take",
        "do you deliver across India",
        "what's the lead time from order to delivery",
        "is there a local pickup option",
        "do you handle your own courier or use third party",
    ],
    "confirmation_signal": [
        "yes that time works for me",
        "go ahead and confirm the booking",
        "lock that slot in",
        "that's confirmed from my side",
        "yes I'll be there",
        "sounds good, see you then",
    ],
}

_OBJECTION_INTENT_TO_LIBRARY_KEY: dict[str, str] = {
    "objection_busy": "busy_now",
    "objection_no_budget": "no_budget",
    "objection_send_info": "send_email",
    "objection_not_interested": "not_interested",
    "objection_existing_vendor": "already_have_vendor",
}

_SKIP_INJECTION_INTENTS = frozenset({"opt_out", "human_request", "anger_escalation"})

SIMILARITY_THRESHOLD = 0.60

_model: Any = None
_anchor_embeddings: dict[str, np.ndarray] | None = None


def preload_intent_model() -> None:
    """Load embedding model and precompute anchor vectors (call at startup)."""
    global _model, _anchor_embeddings
    if _anchor_embeddings is not None:
        return
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer("all-MiniLM-L6-v2")
    _anchor_embeddings = {
        intent: _model.encode(anchors, normalize_embeddings=True).mean(axis=0)
        for intent, anchors in INTENT_EMBEDDING_ANCHORS.items()
    }
    print("[INTENT] Embedding model loaded and anchors precomputed", flush=True)


def detect_intents(utterance: str) -> list[str]:
    """Keyword match first, then embedding similarity for misses."""
    matched: set[str] = set()
    lower = utterance.lower()

    for intent, pattern in INTENT_PATTERNS.items():
        if re.search(pattern, lower):
            matched.add(intent)

    remaining = [i for i in INTENT_PATTERNS if i not in matched]
    if remaining and _model is not None and _anchor_embeddings is not None:
        utt_vec = _model.encode([utterance], normalize_embeddings=True)[0]
        for intent in remaining:
            anchor = _anchor_embeddings.get(intent)
            if anchor is None:
                continue
            sim = float(np.dot(utt_vec, anchor))
            if sim >= SIMILARITY_THRESHOLD:
                matched.add(intent)

    return sorted(matched)


def build_intent_context(
    intents: list[str],
    session: ConversationSession | None,
    call_cfg: dict,
) -> str:
    """Build markdown blocks to append to the turn prompt based on detected intents."""
    if not intents:
        return ""

    sections: dict[str, str] = {}
    perks = (call_cfg.get("perks_of_product") or "").strip()
    objection_library = (
        (call_cfg.get("agent_config") or {}).get("objectionHandling") or {}
    ).get("objectionLibrary") or {}

    for intent in intents:
        if intent in _SKIP_INJECTION_INTENTS:
            continue

        if intent in ("booking_intent", "confirmation_signal") and session:
            compact = format_slots_compact(session.available_slots)
            if compact:
                lines = [compact]
                if session.offered_slots:
                    lines.append(
                        f"Currently offering: {', '.join(session.offered_slots)}"
                    )
                if session.meet_scheduled:
                    lines.append(f"Selected slot: {session.meet_scheduled}")
                sections["Booking"] = "\n".join(lines)

        elif intent in (
            "pricing_intent",
            "product_intent",
            "order_quantity_intent",
            "logistics_intent",
        ):
            if perks:
                sections["Product offer"] = perks

        elif intent.startswith("objection_"):
            lib_key = _OBJECTION_INTENT_TO_LIBRARY_KEY.get(intent)
            guidance = objection_library.get(lib_key or "") if lib_key else ""
            if guidance:
                sections["Objection guidance"] = guidance

        elif intent == "positive_signal" and session:
            stage_key = session.current_stage_key
            if stage_key in ("greeting", "discovery"):
                sections["Positive signal"] = (
                    "Lead is showing interest — briefly acknowledge and "
                    "move toward discovery or booking."
                )
            elif stage_key == "slot_suggestion":
                sections["Positive signal"] = (
                    "Lead is receptive — offer meeting time options now."
                )

        elif intent == "callback_request":
            sections["Callback request"] = (
                "Lead wants a callback — acknowledge, ask when works best, "
                "and confirm you will follow up."
            )

    if not sections:
        return ""

    blocks = [f"## {title}\n{body}" for title, body in sections.items()]
    return "\n\n".join(blocks)
