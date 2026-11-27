"""Picture-card choices for the simple visit flow.

The simple flow replaces SOP step execution: the manager enters the customer's
name and phone, assigns one staff member per category (Hair, Skin, optional
Body), and the staff member taps one picture card to record the customer's type.

Turn the whole flow off with SIMPLE_VISIT_FLOW=false; the SOP flow and all of
its data are untouched. SIMPLE_VISIT_BODY=false hides the Body category.
"""
from django.conf import settings

UNKNOWN = "UNKNOWN"

CATEGORIES = {
    "HAIR": {
        "label": "Hair",
        "question": "What is the customer's hair type?",
        "customer_field": "hair_type",
        "concerns": [
            ("DANDRUFF", "Dandruff"), ("HAIR_FALL", "Hair fall"), ("FRIZZY", "Dry / frizzy"),
            ("OILY_SCALP", "Oily scalp"), ("SPLIT_ENDS", "Split ends"), ("GREY", "Grey hair"),
            ("COLOURED", "Colour treated"),
        ],
        "options": [
            ("STRAIGHT", "Straight", "Falls flat, no bend", "img/types/hair-straight.png"),
            ("WAVY", "Wavy", "Loose S-shaped waves", "img/types/hair-wavy.png"),
            ("CURLY", "Curly", "Springy, defined curls", "img/types/hair-curly.png"),
            ("COILY", "Coily", "Tight coils or zig-zags", "img/types/hair-coily.png"),
        ],
    },
    "SKIN": {
        "label": "Skin",
        "question": "What is the customer's skin type?",
        "customer_field": "skin_type",
        "concerns": [
            ("ACNE", "Acne / pimples"), ("PIGMENTATION", "Pigmentation"), ("TAN", "Tanning"),
            ("DULL", "Dullness"), ("PORES", "Open pores"), ("FINE_LINES", "Fine lines"),
            ("DARK_CIRCLES", "Dark circles"),
        ],
        "options": [
            ("NORMAL", "Normal", "Even, not oily or dry", "img/types/skin-normal.png"),
            ("DRY", "Dry", "Tight, flaky or rough", "img/types/skin-dry.png"),
            ("OILY", "Oily", "Shiny all over", "img/types/skin-oily.png"),
            ("COMBINATION", "Combination", "Oily T-zone, dry cheeks", "img/types/skin-combination.png"),
            ("SENSITIVE", "Sensitive", "Redness, reacts easily", "img/types/skin-sensitive.png"),
        ],
    },
    "BODY": {
        "label": "Body",
        "question": "What is the hand or foot skin like?",
        "customer_field": "body_type",
        "concerns": [
            ("TAN", "Tanning"), ("CUTICLES", "Dry cuticles"), ("NAIL_DAMAGE", "Weak / damaged nails"),
            ("FUNGAL", "Nail discolouration"), ("ODOUR", "Foot odour"),
        ],
        "options": [
            ("NORMAL", "Normal", "Soft and even", "img/types/body-normal.svg"),
            ("DRY", "Dry", "Dull, dry patches", "img/types/body-dry.svg"),
            ("CRACKED", "Cracked heels", "Visible cracks", "img/types/body-cracked.svg"),
            ("CALLOUSED", "Rough / calloused", "Hard, thick skin", "img/types/body-calloused.svg"),
        ],
    },
}

CATEGORY_CHOICES = [(key, value["label"]) for key, value in CATEGORIES.items()]


def simple_flow_enabled():
    return getattr(settings, "SIMPLE_VISIT_FLOW", True)


def enabled_categories():
    keys = ["HAIR", "SKIN"]
    if getattr(settings, "SIMPLE_VISIT_BODY", True):
        keys.append("BODY")
    return keys


def option_codes(category):
    return {code for code, *_ in CATEGORIES[category]["options"]} | {UNKNOWN}


def type_label(category, code):
    if not code:
        return ""
    if code == UNKNOWN:
        return "Not sure"
    for option_code, label, *_ in CATEGORIES.get(category, {}).get("options", []):
        if option_code == code:
            return label
    return code.title()


def type_image(category, code):
    for option_code, _label, _hint, image in CATEGORIES.get(category, {}).get("options", []):
        if option_code == code:
            return image
    return "img/types/unknown.svg"


def concern_codes(category):
    return {code for code, _label in CATEGORIES[category]["concerns"]}


def concern_labels(category, codes):
    names = dict(CATEGORIES.get(category, {}).get("concerns", []))
    return [names.get(code, code.title()) for code in codes or []]
