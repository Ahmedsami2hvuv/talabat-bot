# -*- coding: utf-8 -*-
"""إدارة مناطق التوصيل وأسعارها."""
import os
import re
import json
import difflib

ZONES_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "delivery_zones.json")


def load_delivery_zones():
    """تحميل ملف المناطق وأسعار التوصيل."""
    try:
        os.makedirs(os.path.dirname(ZONES_FILE), exist_ok=True)
        if os.path.exists(ZONES_FILE):
            with open(ZONES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"Error loading delivery zones: {e}")
    return {}


def _longest_zone_in_text(text, zones_dict=None):
    """يرجع أطول منطقة موجودة في النص (عشان كوت الصلحي ما يطابق الـ «الحي» أولاً وتطلع 3 بدل 5)."""
    if not text or not str(text).strip():
        return None
    text = str(text).strip()
    zones = zones_dict or load_delivery_zones()
    best_zone = None
    for zone in zones.keys():
        if zone and zone in text:
            if best_zone is None or len(zone) > len(best_zone):
                best_zone = zone
    return best_zone


def get_delivery_price(address):
    """استخراج سعر التوصيل بناءً على العنوان — نستخدم أطول منطقة مطابقة (كوت الصلحي قبل الحي)."""
    delivery_zones = load_delivery_zones()
    zone = _longest_zone_in_text(address, delivery_zones)
    return delivery_zones[zone] if zone else 0


def is_zone_known(address):
    """هل العنوان يطابق أي منطقة مسجلة في قاعدة البيانات؟"""
    return _longest_zone_in_text(address) is not None


def get_matching_zone_name(text):
    """يدور في النص ويرجع أطول منطقة تظهر فيه (كوت الصلحي قبل الحي)."""
    return _longest_zone_in_text(text)


def get_closest_zone_name(text, cutoff=0.45):
    """
    يقارن الكلمة مع أسماء المناطق ويرجع أقرب منطقة (استعمال قديم، لو حاب منطقة وحدة).
    """
    names = get_closest_zone_names(text, n=1, cutoff=cutoff)
    return names[0] if names else None


def get_closest_zone_names(text, n=6, cutoff=0.4):
    """
    يرجع قائمة بأسماء المناطق الأقرب للكلمة (أكثر من كلمة).
    n: أقصى عدد مناطق، cutoff: أقل نسبة تشابه.
    """
    if not text or not str(text).strip():
        return []
    try:
        delivery_zones = load_delivery_zones()
        zone_names = [str(k) for k in delivery_zones.keys() if k]
    except Exception:
        return []
    if not zone_names:
        return []
    text_clean = str(text).strip()
    return difflib.get_close_matches(text_clean, zone_names, n=n, cutoff=cutoff)


def match_text_to_suggested_zones(text, suggested_zone_names, cutoff=0.8):
    """
    إذا المستخدم كتب اسم منطقة بدل ما يضغط الزر، نطابق كتابته مع قائمة المناطق المقترحة.
    يرجع (index, matched_zone) إذا النص مطابق جداً لأحد المناطق، وإلا None.
    """
    if not text or not suggested_zone_names:
        return None
    text_clean = str(text).strip()
    if not text_clean:
        return None
    matches = difflib.get_close_matches(text_clean, suggested_zone_names, n=1, cutoff=cutoff)
    if not matches:
        return None
    matched = matches[0]
    for i, z in enumerate(suggested_zone_names):
        if z == matched:
            return (i, matched)
    return None


def get_all_close_zones_from_words(full_text, per_word_n=4, cutoff=0.4):
    """
    يقارن كل كلمة في النص بقاعدة المناطق، ويرجع كل المناطق اللي ممكن تكون قريبة من أي كلمة.
    يرجع قائمة بدون تكرار. لو صار خطأ يرجع قائمة فاضية.
    """
    pairs = get_close_zones_with_words(full_text, per_word_n=per_word_n, cutoff=cutoff)
    return [zone for zone, _ in pairs]


def get_close_zones_with_words(full_text, per_word_n=2, cutoff=0.5, max_zones_per_word=1):
    """
    يقارن أسطر الرسالة (كلمة وحدة أو كلمتين) بقاعدة المناطق، ويرجع (منطقة، نص السطر).
    cutoff 0.5 عشان كوت صحي→كوت الصلحي، بي عسكري→حي العسكري يطابقون.
    """
    if not full_text or not str(full_text).strip():
        return []
    try:
        # خطوة أولى: نتجاهل الأسطر اللي فيها أرقام
        # خطوة ثانية: أسطر 3 كلمات فأكثر — ما ناخذ السطر كامل، لكن ناخذ أول كلمة وأول كلمتين (عشان سطر العنوان مثل "كوت تويني القرب نقطة...")
        candidate_phrases = []
        for line in str(full_text).strip().split("\n"):
            line = (line or "").strip()
            if not line:
                continue
            if re.search(r"\d", line):
                continue
            tokens = line.split()
            if len(tokens) == 0:
                continue
            if len(tokens) == 1:
                phrase = tokens[0]
                if len(phrase) < 2 or phrase.startswith("+") or all(c in "0123456789+" for c in phrase):
                    continue
                candidate_phrases.append(phrase)
            elif len(tokens) == 2:
                phrase = " ".join(tokens)
                if len(phrase) < 2 or phrase.startswith("+") or all(c in "0123456789+ " for c in phrase):
                    continue
                candidate_phrases.append(phrase)
            else:
                # سطر طويل (مثل العنوان): ناخذ أول كلمتين ثم أول كلمة — عشان "كوت تويني" تطلع قبل "كوت"
                two = " ".join(tokens[:2])
                if len(two) >= 2 and not two.startswith("+"):
                    candidate_phrases.append(two)
                w0 = tokens[0]
                if len(w0) >= 2 and not w0.startswith("+") and not all(c in "0123456789+" for c in w0) and w0 != two:
                    candidate_phrases.append(w0)
        seen_zones = set()
        result = []  # [(zone, phrase), ...]
        # مطابقة بقوة: cutoff عالي عشان نجيبلهم الاقرب (حرف/حرفين غلط)
        strong_cutoff = cutoff
        for phrase in candidate_phrases:
            zones = get_closest_zone_names(phrase, n=per_word_n, cutoff=strong_cutoff)
            added = 0
            for z in zones:
                if z and z not in seen_zones and added < max_zones_per_word:
                    seen_zones.add(z)
                    result.append((z, phrase))
                    added += 1
        # ضمان حوجة → عوجة (لو المستخدم كتب سطر "حوجة" وما طابقت)
        if "حوجة" in candidate_phrases and not any(w == "حوجة" for _, w in result):
            zones_map = load_delivery_zones()
            for alias in ("عوجه", "عوجة", "العوجة", "العوجه"):
                if alias in zones_map and alias not in seen_zones:
                    result.append((alias, "حوجة"))
                    break
        for i, (z, w) in enumerate(result):
            if w == "حوجة":
                result.insert(0, result.pop(i))
                break
        return result
    except Exception:
        return []


async def list_zones(update, context):
    """عرض قائمة المناطق وأسعار التوصيل (أمر /zones أو كلمة مناطق)."""
    zones = load_delivery_zones()
    if not zones:
        await update.message.reply_text("ماكو مناطق مسجلة حالياً. أضف ملف data/delivery_zones.json")
        return
    lines = ["مناطق التوصيل وأسعارها:", "-----------------------------------"]
    for zone, price in zones.items():
        lines.append(f"• {zone}: {price} دينار")
    await update.message.reply_text("\n".join(lines))
