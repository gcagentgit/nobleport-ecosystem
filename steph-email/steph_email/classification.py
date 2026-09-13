"""Explainable rules. No trained model or accuracy claim is implied."""
import re
from .models import sender_matches

KEYWORDS = {
    "urgent": 18, "asap": 14, "emergency": 40, "injury": 40, "stop work": 40,
    "safety": 20, "inspection": 12, "failed inspection": 30, "deadline": 20,
    "due today": 30, "due tomorrow": 22, "action required": 20,
    "overdue": 18, "past due": 20, "final notice": 25, "payment due": 15,
    "change order": 12, "additional work order": 15, "bid": 8, "invoice": 6,
    "drawings": 8, "permit": 10, "credit": 8, "shutdown": 30,
}
SPAM_PHRASES = ("you have won", "claim your prize", "guaranteed returns", "lottery winner")


def contains(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text, re.I) is not None


def classify(email, rules, settings):
    subject, body = email.subject[:1000], email.text[:20000]
    score, reasons = 0, []
    if settings["urgency_detection"]:
        words = {**KEYWORDS, **rules.get("urgent_keywords", {})}
        for phrase, weight in words.items():
            if not isinstance(weight, (int, float)) or not 0 <= weight <= 100:
                continue
            if contains(subject, phrase):
                score += round(weight * 1.5)
                reasons.append(f"Subject: {phrase}")
            elif contains(body, phrase):
                score += weight
                reasons.append(f"Message: {phrase}")
        if any(sender_matches(p, email.sender) for p in rules.get("priority_senders", [])):
            score += 15
            reasons.append("Known sender address/domain match; identity not verified")
    score = min(100, int(score))
    spam_reasons = []
    if settings["spam_filtering"]:
        if any(sender_matches(p, email.sender) for p in rules.get("blocked_senders", [])):
            spam_reasons.append("Blocked sender rule")
        hits = [p for p in (*SPAM_PHRASES, *rules.get("spam_phrases", [])) if contains(subject + " " + body, p)]
        if hits:
            spam_reasons.append("Suspicious phrase: " + ", ".join(hits))
    return {"urgency_score": score, "reasons": reasons + spam_reasons,
            "is_urgent": score >= settings["urgency_threshold"], "is_spam": bool(spam_reasons)}
