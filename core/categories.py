import re
import unicodedata

from core.saved_offer_taxonomy import (
    SAVED_OFFER_SUBCATEGORIES_BY_FILTER,
)


# Names returned by AliExpress marketplace/category APIs. Search keeps a small
# stable internal family key, while these official names remain stored verbatim.
OFFICIAL_ALIEXPRESS_TOP_LEVEL_CATEGORIES = (
    "Phones & Telecommunications",
    "Consumer Electronics",
    "Computer & Office",
    "Home Appliances",
    "Home & Garden",
    "Luggage & Bags",
    "Women's Clothing",
    "Men's Clothing",
    "Mother & Kids",
    "Jewelry & Accessories",
    "Shoes",
    "Sports & Entertainment",
    "Watches",
    "Beauty, Health & Hair",
    "Hair Extensions & Wigs",
    "Toys, Kids & Babies",
    "Tools",
    "Home Improvement",
    "Lights & Lighting",
    "Furniture",
    "Security & Protection",
    "Automobiles & Motorcycles",
    "Motorcycle Equipments & Parts",
    "Electronic Components & Supplies",
    "Education & Office Supplies",
    "Pet Products",
    "Food",
    "Apparel Accessories",
    "Underwear & Sleepwear",
    "Novelty & Special Use",
    "Weddings & Events",
)

SAVED_OFFER_CATEGORY_LABELS = {
    "Phones & Telecommunications": "هواتف",
    "Consumer Electronics": "إلكترونيات",
    "Computer & Office": "كمبيوتر",
    "Home Appliances": "منزلية",
    "Home & Garden": "منزل",
    "Luggage & Bags": "حقائب",
    "Women's Clothing": "ملابس نسائية",
    "Men's Clothing": "ملابس رجالية",
    "Mother & Kids": "أم وطفل",
    "Jewelry & Accessories": "مجوهرات",
    "Shoes": "أحذية",
    "Sports & Entertainment": "رياضة",
    "Watches": "ساعات",
    "Beauty, Health & Hair": "جمال وصحة",
    "Hair Extensions & Wigs": "شعر مستعار",
    "Toys, Kids & Babies": "ألعاب وأطفال",
    "Tools": "أدوات",
    "Home Improvement": "تحسين المنزل",
    "Lights & Lighting": "إضاءة",
    "Furniture": "أثاث",
    "Security & Protection": "أمان",
    "Automobiles & Motorcycles": "سيارات",
    "Motorcycle Equipments & Parts": "دراجات نارية",
    "Electronic Components & Supplies": "مكونات",
    "Education & Office Supplies": "مكتبية وتعليم",
    "Pet Products": "حيوانات أليفة",
    "Food": "أغذية",
    "Apparel Accessories": "إكسسوارات ملابس",
    "Underwear & Sleepwear": "داخلية ونوم",
    "Novelty & Special Use": "متنوعة",
    "Weddings & Events": "أعراس ومناسبات",
}

SPECIAL_CATEGORY_SMART_WATCHES = "smart_watches"
SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES = "bluetooth_headphones"
SAVED_OFFER_CATEGORY_COLUMN_FILTERS = frozenset({
    SPECIAL_CATEGORY_SMART_WATCHES,
    SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES,
})

# Saved-offer browsing keeps five subcategory families as individual buttons
# and groups the remaining official second-level families into the user-facing
# families below. The values are normalized English fragments found in
# AliExpress second-level category names, never first-level category names.
SAVED_OFFER_CATEGORY_GROUPS = (
    (
        "saved_home",
        "المنزل",
        (
            "home",
            "garden",
            "kitchen",
            "appliance",
            "furniture",
            "decor",
            "improvement",
        ),
    ),
    (
        "saved_children",
        "الأطفال",
        (
            "mother",
            "kids",
            "baby",
            "toy",
        ),
    ),
    (
        "saved_fashion",
        "الأزياء",
        (
            "clothing",
            "apparel",
            "shoes",
            "underwear",
            "sleepwear",
            "dress",
            "shirt",
            "pants",
        ),
    ),
    (
        "saved_accessories",
        "الإكسسوارات",
        (
            "accessories",
            "bags",
            "luggage",
            "jewelry",
            "watch",
            "watches",
            "case",
            "charger",
            "cable",
        ),
    ),
    (
        "saved_beauty",
        "الجمال",
        (
            "beauty",
            "health",
            "hair",
            "wig",
            "cosmetic",
            "skin",
        ),
    ),
    (
        "saved_vehicles",
        "المركبات",
        (
            "automobile",
            "motorcycle",
            "vehicle",
            "car",
        ),
    ),
    (
        "saved_tools",
        "الأدوات",
        (
            "light",
            "lighting",
            "security",
            "protection",
            "tool",
        ),
    ),
    (
        "saved_miscellaneous",
        "متنوعات",
        (
            "pet",
            "education",
            "office",
            "component",
            "food",
            "wedding",
            "event",
            "novelty",
        ),
    ),
    ("saved_spare_parts", "قطع غيار", ("spare part",)),
    (
        "saved_electronic_supplies",
        "مستلزمات إلكترونية",
        ("electronic accessory", "electronic component"),
    ),
)

SAVED_OFFER_CATEGORY_GROUPS_BY_KEY = {
    key: categories
    for key, _label, categories in SAVED_OFFER_CATEGORY_GROUPS
}

# The individual entries and grouped entries all resolve through
# second_level_category_name. Keeping stable internal keys here avoids putting
# first-level names into search intents.
SAVED_OFFER_CATEGORY_FILTERS = (
    ("saved_phones", "هواتف"),
    ("saved_tablets", "طابلات"),
    ("saved_electronics", "أجهزة الكترونية"),
    ("saved_computer", "كمبيوتر"),
    (SPECIAL_CATEGORY_SMART_WATCHES, "ساعات ذكية"),
    (SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES, "سماعات بلوثوث"),
    ("saved_sports", "رياضة"),
) + tuple(
    (key, label)
    for key, label, _categories in SAVED_OFFER_CATEGORY_GROUPS
)

SAVED_OFFER_SUBCATEGORY_PATTERNS = {
    "saved_phones": (
        "mobile phone",
        "mobile phones",
        "smartphone",
        "smartphones",
        "cell phone",
    ),
    "saved_tablets": ("tablet", "ipad"),
    "saved_electronics": (
        "consumer electronic",
        "earphone",
        "headphone",
        "headset",
        "earbud",
        "camera",
        "photo",
        "video game",
        "game console",
        "audio",
        "speaker",
    ),
    "saved_computer": (
        "laptop",
        "notebook",
        "desktop computer",
        "computer peripheral",
        "computer component",
        "storage device",
        "networking",
    ),
    "saved_sports": (
        "sports",
        "outdoor",
        "fitness",
        "cycling",
        "leisure",
        "entertainment",
    ),
}

SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES = {
    **SAVED_OFFER_SUBCATEGORIES_BY_FILTER,
    "saved_phones": ("Mobile Phones", "Used&Refurbished Phones", "Used Phones"),
    "saved_tablets": ("Tablets",),
}
SAVED_OFFER_PROVIDER_SUBCATEGORY_NAMES = tuple(
    dict.fromkeys(
        name
        for names in SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES.values()
        for name in names
    )
)


def saved_offer_filter_from_subcategory(second_level_name):
    """Map an AliExpress second-level name to a saved-offer filter key."""
    second = _normalize_category_name(second_level_name)
    if not second or second in {"غير محدد", "undefined", "unknown"}:
        return None

    # Explicit AliExpress names are authoritative and prevent broad legacy
    # fragments (for example "watch" or "accessories") from crossing groups.
    for key, names in SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES.items():
        if any(second == _normalize_category_name(name) for name in names):
            return key

    # Keep phone accessories out of the phone button even though AliExpress
    # places both families under the same first-level category.
    if any(
        term in second
        for term in (
            "mobile phone accessories",
            "phone accessories",
            "mobile phone parts",
            "phone parts",
            "cases",
            "chargers",
            "cables",
        )
    ):
        return "saved_accessories"

    for key in ("saved_spare_parts", "saved_electronic_supplies"):
        patterns = SAVED_OFFER_CATEGORY_GROUPS_BY_KEY[key]
        if any(pattern in second for pattern in patterns):
            return key

    for key, patterns in SAVED_OFFER_SUBCATEGORY_PATTERNS.items():
        if any(pattern in second for pattern in patterns):
            return key

    for key, _label, patterns in SAVED_OFFER_CATEGORY_GROUPS:
        if any(pattern in second for pattern in patterns):
            return key
    return None


_INTERNAL_CATEGORY_OFFICIAL_NAMES = {
    "phones": ("Phones & Telecommunications", "Mobile Phones"),
    "accessories": (
        "Phones & Telecommunications",
        "Mobile Phone Accessories",
    ),
    "headphones": ("Consumer Electronics", "Earphones & Headphones"),
    "tablets": ("Computer & Office", "Tablets"),
    "laptops": ("Computer & Office", "Laptops"),
    "watches": ("Watches", "Smart Watches"),
    SPECIAL_CATEGORY_SMART_WATCHES: ("Watches", "Smart Watches"),
    SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES: (
        "Consumer Electronics",
        "Earphones & Headphones",
    ),
    "cameras": ("Consumer Electronics", "Camera & Photo"),
    "gaming": ("Consumer Electronics", "Video Games"),
    "home": ("Home Appliances", "Home Appliances"),
}


def official_names_for_internal_category(category):
    """Return canonical AliExpress taxonomy names for a validated AI family."""
    names = _INTERNAL_CATEGORY_OFFICIAL_NAMES.get(str(category or "").casefold())
    if not names:
        return None
    return {
        "first_level_category_name": names[0],
        "second_level_category_name": names[1],
    }


def canonical_saved_offer_subcategory_name(value):
    """Return an allowed saved-offer name or a preserved special name."""
    normalized = _normalize_category_name(value)
    if not normalized:
        return None

    for names in SAVED_OFFER_CATEGORY_SUBCATEGORY_NAMES.values():
        for name in names:
            if normalized == _normalize_category_name(name):
                return name

    # These two names support the existing dedicated smart-watch and
    # Bluetooth-headphone classification paths, but are not general saved
    # offer subcategories in the supplied taxonomy.
    for special_category in (
        SPECIAL_CATEGORY_SMART_WATCHES,
        SPECIAL_CATEGORY_BLUETOOTH_HEADPHONES,
    ):
        _first_level, second_level = _INTERNAL_CATEGORY_OFFICIAL_NAMES[
            special_category
        ]
        if normalized == _normalize_category_name(second_level):
            return second_level
    return None


def _normalize_category_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[\w]+", text))


def category_from_aliexpress_names(first_level_name, second_level_name):
    """Map official AliExpress hierarchy names to a search family.

    Only the second-level name is authoritative. Broad first-level categories
    such as Phones & Telecommunications contain unrelated product families.
    """
    second = _normalize_category_name(second_level_name)
    if not second or second in {"غير محدد", "undefined", "unknown"}:
        return None

    if any(
        term in second
        for term in (
            "mobile phone accessories",
            "phone accessories",
            "mobile phone parts",
            "cases",
            "chargers",
            "cables",
        )
    ):
        return "accessories"
    if any(term in second for term in ("earphone", "headphone", "headset", "earbud")):
        return "headphones"
    if any(term in second for term in ("smart watch", "smartwatch", "wristwatch")):
        return "watches"
    if any(term in second for term in ("mobile phones", "smartphones")):
        return "phones"
    if "tablet" in second:
        return "tablets"
    if any(term in second for term in ("laptop", "notebook")):
        return "laptops"
    if any(term in second for term in ("camera", "photo")):
        return "cameras"
    if any(term in second for term in ("video game", "game console")):
        return "gaming"

    if any(
        term in second
        for term in (
            "home appliance",
            "home garden",
            "home improvement",
            "kitchen",
        )
    ):
        return "home"
    return None