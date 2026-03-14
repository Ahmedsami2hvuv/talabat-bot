# -*- coding: utf-8 -*-
"""تصنيف المنتجات: سمك، خضروات وفواكه (حسب ملفات data)."""
import os
import re

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VEG_FILE = os.path.join(_BASE_DIR, "data", "vegetables_fruits.txt")
_FISH_FILE = os.path.join(_BASE_DIR, "data", "fish_types.txt")
_MEAT_FILE = os.path.join(_BASE_DIR, "data", "meat_types.txt")

_veg_words = None
_fish_words = None
_meat_words = None


def _load_lines(filepath):
    """قراءة أسطر الملف (كلمة واحدة أو أكثر بسطر)، تجاهل الفارغة."""
    out = []
    if not os.path.exists(filepath):
        return out
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if w:
                    out.append(w)
    except Exception:
        pass
    return out


def _get_veg_words():
    global _veg_words
    if _veg_words is None:
        _veg_words = _load_lines(_VEG_FILE)
    return _veg_words


def _get_fish_words():
    global _fish_words
    if _fish_words is None:
        _fish_words = _load_lines(_FISH_FILE)
    return _fish_words


def _get_meat_words():
    global _meat_words
    if _meat_words is None:
        _meat_words = _load_lines(_MEAT_FILE)
    return _meat_words


def is_meat(product_name):
    """إذا اسم المنتج يحتوي على أي كلمة من ملف اللحم (لحم، شرح، مثروم، عظم، باجه، شحم) يعتبر لحم."""
    if not product_name or not product_name.strip():
        return False
    p = product_name.strip()
    for w in _get_meat_words():
        if w in p:
            return True
    return False


def is_vegetable_fruit(product_name):
    """إذا اسم المنتج يحتوي على أي كلمة من ملف الخضروات/الفواكه يعتبر خضروات أو فواكه."""
    if not product_name or not product_name.strip():
        return False
    p = product_name.strip()
    for w in _get_veg_words():
        if w in p:
            return True
    return False


def is_fish(product_name):
    """إذا اسم المنتج يبدأ بـ سمك أو يحتوي على أي نوع من ملف السمك يعتبر سمك."""
    if not product_name or not product_name.strip():
        return False
    p = product_name.strip()
    fish_list = _get_fish_words()
    if not fish_list:
        return p.startswith("سمك")
    if p.startswith("سمك"):
        return True
    for w in fish_list:
        if w in p:
            return True
    return False


def reload_categories():
    """إعادة تحميل القوائم من الملفات (مفيد بعد تعديل الملفات)."""
    global _veg_words, _fish_words, _meat_words
    _veg_words = None
    _fish_words = None
    _meat_words = None
    _get_veg_words()
    _get_fish_words()
    _get_meat_words()
