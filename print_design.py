import copy
import json
import re

from database import get_setting, set_setting


PRINT_STYLE_SETTING_KEYS = {
    "invoice": "invoice_print_style_json",
    "collection": "collection_header_style_json",
    "reading": "reading_header_style_json",
}


INVOICE_STYLE_FIELDS = [
    ("stub_org", "الكعب — اسم المنظمة والعنوان"),
    ("stub_subscriber_label", "الكعب — تسمية المشترك"),
    ("stub_subscriber_value", "الكعب — اسم المشترك"),
    ("stub_month_label", "الكعب — تسمية الشهر / العداد"),
    ("stub_month_value", "الكعب — الشهر / العداد"),
    ("stub_number_label", "الكعب — تسمية رقم الفاتورة"),
    ("stub_number_value", "الكعب — رقم الفاتورة"),
    ("stub_region_label", "الكعب — تسمية المنطقة"),
    ("stub_region_value", "الكعب — المنطقة"),
    ("stub_due_label", "الكعب — تسمية المبلغ المستحق"),
    ("stub_due_value", "الكعب — المبلغ المستحق"),
    ("stub_tafqit", "الكعب — التفقيط"),
    ("stub_notes_label", "الكعب — تسمية الملاحظات"),
    ("stub_notes_value", "الكعب — الملاحظات"),
    ("stub_created_label", "الكعب — تسمية أُصدرت بواسطة"),
    ("stub_created_value", "الكعب — أُصدرت بواسطة"),
    ("stub_date_label", "الكعب — تسمية التاريخ"),
    ("stub_date_value", "الكعب — التاريخ"),
    ("stub_engineer_label", "الكعب — تسمية البرمجة والتصميم"),
    ("stub_engineer_value", "الكعب — البرمجة والتصميم"),
    ("main_title", "الفاتورة الرئيسية — نوع الفاتورة"),
    ("main_org", "الفاتورة الرئيسية — اسم المنظمة"),
    ("main_month_label", "الفاتورة الرئيسية — تسمية الشهر"),
    ("main_month_value", "الفاتورة الرئيسية — الشهر"),
    ("main_region_label", "الفاتورة الرئيسية — تسمية المنطقة / العنوان"),
    ("main_region_value", "الفاتورة الرئيسية — المنطقة / العنوان"),
    ("main_name_label", "الفاتورة الرئيسية — تسمية اسم المشترك"),
    ("main_name_value", "الفاتورة الرئيسية — اسم المشترك"),
    ("main_account_label", "بيانات المشترك — تسمية رقم المشترك"),
    ("main_account_value", "بيانات المشترك — رقم المشترك"),
    ("main_meter_label", "بيانات المشترك — تسمية رقم العداد"),
    ("main_meter_value", "بيانات المشترك — رقم العداد"),
    ("main_connection_label", "بيانات المشترك — تسمية حالة التوصيل"),
    ("main_connection_value", "بيانات المشترك — حالة التوصيل"),
    ("main_invoice_label", "بيانات الفاتورة — تسمية رقم الفاتورة"),
    ("main_invoice_value", "بيانات الفاتورة — رقم الفاتورة"),
    ("main_period_label", "بيانات الفاتورة — تسمية فترة الاستهلاك"),
    ("main_period_value", "بيانات الفاتورة — فترة الاستهلاك"),
    ("readings_header", "جدول الحساب — عناوين القراءات والاستهلاك"),
    ("readings_value", "جدول الحساب — القيم"),
    ("bottom_consumption_label", "أسفل الفاتورة — تسمية قيمة الاستهلاك"),
    ("bottom_consumption_value", "أسفل الفاتورة — قيمة الاستهلاك"),
    ("bottom_total_label", "أسفل الفاتورة — تسمية الإجمالي المستحق"),
    ("bottom_total_value", "أسفل الفاتورة — الإجمالي المستحق"),
    ("footer_note", "أسفل الفاتورة — ملاحظة السداد"),
    ("footer_design", "ذيل الفاتورة — برمجة وتصميم"),
]

COLLECTION_STYLE_FIELDS = [
    ("topline_label", "الخانات العلوية — العناوين"),
    ("topline_value", "الخانات العلوية — القيم"),
    ("summary_label", "خانات الملخص — العناوين"),
    ("summary_value", "خانات الملخص — القيم"),
    ("table_header", "رأس كشف التحصيل — عناوين الجدول"),
]

READING_STYLE_FIELDS = [
    ("title", "رأس كشف القراءة — عنوان الكشف"),
    ("info_label", "الخانات العلوية — العناوين"),
    ("info_value", "الخانات العلوية — القيم"),
    ("table_header", "رأس كشف القراءة — عناوين الجدول"),
]


def _style(bg="#ffffff", text="#000000"):
    return {"bg": bg, "text": text}


def _invoice_defaults():
    label = _style("#f2f2f2", "#000000")
    value = _style("#ffffff", "#000000")
    table_header = _style("#f2f2f2", "#000000")
    data = {}
    for key, _ in INVOICE_STYLE_FIELDS:
        if key == "stub_org":
            data[key] = _style("#ffffff", "#000000")
        elif key in {
            "main_title", "main_org", "stub_tafqit", "footer_note", "footer_design",
            "readings_value", "bottom_consumption_value", "bottom_total_value",
        }:
            data[key] = _style("#ffffff", "#000000")
        elif key == "readings_header":
            data[key] = copy.deepcopy(table_header)
        elif key.startswith(("stub_", "main_", "bottom_")) and key.endswith("_label"):
            data[key] = copy.deepcopy(label)
        elif key.endswith("_label"):
            data[key] = copy.deepcopy(label)
        else:
            data[key] = copy.deepcopy(value)
    return data


STYLE_DEFAULTS = {
    "invoice": _invoice_defaults(),
    "collection": {
        "topline_label": _style("#e2e8f0", "#334155"),
        "topline_value": _style("#ffffff", "#000000"),
        "summary_label": _style("#f1f5f9", "#475569"),
        "summary_value": _style("#ffffff", "#000000"),
        "table_header": _style("#e2e8f0", "#1e293b"),
    },
    "reading": {
        "title": _style("#f1f5f9", "#111827"),
        "info_label": _style("#e2e8f0", "#334155"),
        "info_value": _style("#ffffff", "#111827"),
        "table_header": _style("#e2e8f0", "#1e293b"),
    },
}


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_color(value, fallback):
    value = str(value or "").strip()
    if _HEX_RE.fullmatch(value):
        return value.lower()
    return fallback


def _merge_section(section, defaults):
    merged = copy.deepcopy(defaults)
    if not isinstance(section, dict):
        return merged
    for key, default_cfg in defaults.items():
        raw_cfg = section.get(key)
        if not isinstance(raw_cfg, dict):
            continue
        merged[key]["bg"] = _clean_color(raw_cfg.get("bg"), default_cfg["bg"])
        merged[key]["text"] = _clean_color(raw_cfg.get("text"), default_cfg["text"])
    return merged


def get_print_style_settings():
    result = {}
    for section, setting_key in PRINT_STYLE_SETTING_KEYS.items():
        defaults = STYLE_DEFAULTS[section]
        raw = get_setting(setting_key, "")
        try:
            stored = json.loads(raw) if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            stored = {}
        result[section] = _merge_section(stored, defaults)
    return result


def save_print_style_settings(payload):
    payload = payload or {}
    normalized = {}
    for section, setting_key in PRINT_STYLE_SETTING_KEYS.items():
        defaults = STYLE_DEFAULTS[section]
        incoming = payload.get(section) if isinstance(payload.get(section), dict) else {}
        section_data = {}
        for key, default_cfg in defaults.items():
            cfg = incoming.get(key) if isinstance(incoming.get(key), dict) else {}
            section_data[key] = {
                "bg": _clean_color(cfg.get("bg"), default_cfg["bg"]),
                "text": _clean_color(cfg.get("text"), default_cfg["text"]),
            }
        normalized[section] = section_data
        set_setting(
            setting_key,
            json.dumps(section_data, ensure_ascii=False, separators=(",", ":")),
        )
    return normalized
