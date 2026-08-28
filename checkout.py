from flask import Blueprint, request, jsonify, session, render_template, abort
from bson import ObjectId
from datetime import datetime, timedelta
import os, uuid, random, requests, traceback, json, ast, re, threading, time

from db import db

checkout_bp = Blueprint("checkout", __name__)

# MongoDB Collections
balances_col        = db["balances"]
orders_col          = db["orders"]
transactions_col    = db["transactions"]
services_col        = db["services"]
service_profits_col = db["service_profits"]  # per-customer overrides
users_col           = db["users"]  # ✅ for invoice view
blocked_phone_numbers_col = db["blocked_phone_numbers"]
known_number_attempts_col = db["known_number_attempts"]
settings_col        = db["settings"]
carts_col           = db["carts"]


# ===== Portal-02 Provider Config ==============================================
PORTAL02_BASE_URL = "https://www.portal-02.com/api/v1"
PORTAL02_API_KEY = os.getenv("PORTAL02_API_KEY", "dk_yqFBqOoZJ3TET49kknXqmVQNabhefJlv")
PORTAL02_WEBHOOK_URL = os.getenv(
    "PORTAL02_WEBHOOK_URL",
    "https://www.portal-02.com/api/webhooks/orders",
)

# ===== DataConnect Provider Config ===========================================
DATACONNECT_BASE_URL = "https://dataconnectgh.com/api/v1"
DATACONNECT_API_KEY = os.getenv("DATACONNECT_API_KEY", "d3ead3a6e67f483e2c18a6bbe5bbc1df9ab8984a")

# ===== CodeCraft Provider Config =============================================
CODECRAFT_BASE_URL = os.getenv("CODECRAFT_BASE_URL", "https://api.codecraftnetwork.com/api")
CODECRAFT_API_KEY = os.getenv("CODECRAFT_API_KEY", "260129025618-iafWYf-|FJJLo-ov1b8V-0?vzDK-AYNMWV").strip()
CODECRAFT_TTL_SEC = 300

# ===== DataKazina Provider Config ============================================
DATAKAZINA_BASE_URL = os.getenv("DATAKAZINA_BASE_URL", "https://reseller.dakazinabusinessconsult.com/api/v1")
DATAKAZINA_API_KEY = os.getenv("DATAKAZINA_API_KEY","dk_KOucd2evniMWSNXEtYiN9GxhTSZn78gd")
DATAKAZINA_TIMEOUT = int(os.getenv("DATAKAZINA_TIMEOUT", "45"))

# ===== SKPlug Provider Config ================================================
SKPLUG_BASE_URL = os.getenv("SKPLUG_BASE_URL", "https://skplug.onrender.com/api/v1/")
SKPLUG_API_KEY = os.getenv("SKPLUG_API_KEY", "3c0e78adb998d64bdd67bc5544cd5bb8d994f7f5812caad709929ea3daab8d5c")
SKPLUG_TIMEOUT = int(os.getenv("SKPLUG_TIMEOUT", "45"))

# ===== BundlePortal Provider Config ==========================================
BUNDLEPORTAL_BASE_URL = os.getenv("BUNDLEPORTAL_BASE_URL", "https://api.bundleportal.com/v1")
BUNDLEPORTAL_API_KEY = os.getenv("BUNDLEPORTAL_API_KEY", "").strip()
BUNDLEPORTAL_TIMEOUT = int(os.getenv("BUNDLEPORTAL_TIMEOUT", "45"))

# Default offer slugs (can be overridden per-service or per-item)
PORTAL02_OFFER_SLUG_MTN_NORMAL = "master_beneficiary_data_bundle"  # MTN normal
PORTAL02_OFFER_SLUG_TELECEL    = "telecel_expiry_bundle"
PORTAL02_OFFER_SLUG_ISHARE     = "ishare_data_bundle"

# Network ID fallback (internal use)
NETWORK_ID_FALLBACK = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}

_CODECRAFT_CACHE = {"ts": 0, "packages": {}}

# ===== Tiny JSON logger =======================================================
def jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")


# ===== Helpers ================================================================
def generate_order_id():
    return f"HAN{random.randint(0, 9_999_999):07d}"


def _single_order_status_from_line(item: dict, fallback: str = "pending") -> str:
    line_status = str((item or {}).get("line_status") or "").strip().lower()
    if line_status in {"skipped_duplicate_processing", "skipped_duplicate_in_cart"}:
        return "skipped"
    if line_status == "delivered":
        return "delivered"
    if line_status == "failed":
        return "failed"
    return fallback


def _persist_split_order_docs(
    *,
    orders_collection,
    results: list[dict],
    base_order_fields: dict,
    api_jobs: list[dict] | None = None,
    propagate_reference_field: str | None = None,
):
    """
    Persist each checkout line as its own order document while preserving the
    existing line payload shape.
    """
    api_jobs = api_jobs or []
    job_map = {
        str(job.get("provider_request_order_id") or "").strip(): job
        for job in api_jobs
        if str(job.get("provider_request_order_id") or "").strip()
    }

    saved_docs = []
    order_jobs = []
    group_order_id = str(base_order_fields.get("order_id") or generate_order_id()).strip()

    for idx, raw_item in enumerate(results or [], start=1):
        item = dict(raw_item or {})
        line_ref = str(item.get("provider_request_order_id") or "").strip()
        line_order_id = generate_order_id()
        total_amount = round(_money(item.get("amount")), 2)
        profit_amount_total = round(_money(item.get("profit_amount")), 2)
        status = _single_order_status_from_line(item, str(base_order_fields.get("status") or "pending"))

        order_doc = dict(base_order_fields)
        order_doc["order_id"] = line_order_id
        order_doc["items"] = [item]
        order_doc["total_amount"] = total_amount
        order_doc["charged_amount"] = total_amount if status != "skipped" else 0.0
        order_doc["profit_amount_total"] = profit_amount_total
        order_doc["status"] = status

        debug_block = dict(order_doc.get("debug") or {})
        debug_block.update(
            {
                "split_from_bulk_save": True,
                "bulk_group_order_id": group_order_id,
                "bulk_group_index": idx,
                "bulk_group_size": len(results or []),
            }
        )
        order_doc["debug"] = debug_block

        if propagate_reference_field and idx > 1:
            order_doc.pop(propagate_reference_field, None)

        orders_collection.insert_one(order_doc)
        saved_docs.append(order_doc)

        if line_ref and line_ref in job_map:
            order_jobs.append((line_order_id, [job_map[line_ref]]))

    return saved_docs, order_jobs


def _money(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _clear_customer_cart(user_id):
    try:
        carts_col.update_one(
            {"user_id": user_id},
            {"$set": {"items": [], "updated_at": datetime.utcnow()}},
            upsert=True,
        )
    except Exception:
        pass


def _to_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def _coerce_value_obj(v):
    """
    Accepts dict, JSON string, or python-dict-like string.
    Returns a dict (possibly empty).
    """
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    s = str(v).strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:
            try:
                d = ast.literal_eval(s)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
    return {}


# ===== API key cleaner =======================================================
def _clean_api_key(raw):
    """
    Remove stray unicode/zero-width characters from API keys before use.
    """
    if raw is None:
        return ""
    try:
        s = str(raw)
    except Exception:
        return ""
    s = re.sub(r"[\uFEFF\u200B-\u200D\u2060]", "", s)
    try:
        s = s.encode("ascii", "ignore").decode("ascii")
    except Exception:
        pass
    return s.strip()


# ===== Profit helpers (absolute profit amount) ================================
def _get_service_default_profit_percent(service_doc):
    return _to_float(service_doc.get("default_profit_percent"), 0.0) or 0.0


def _get_customer_profit_override_percent(service_id, customer_id_obj):
    ov = service_profits_col.find_one({"service_id": service_id, "customer_id": customer_id_obj})
    return _to_float(ov.get("profit_percent"), None) if ov else None


def _effective_profit_percent(service_doc, customer_id_obj):
    override = _get_customer_profit_override_percent(service_doc["_id"], customer_id_obj)
    return override if override is not None else _get_service_default_profit_percent(service_doc)


def _pick_offer_base_amount_from_service(svc_doc, value_obj, raw_value):
    """
    Try to recover the base (wholesale) amount from the selected offer in svc_doc.offers.
    """
    try:
        offers = svc_doc.get("offers") or []
        vid = (value_obj or {}).get("id")
        vvol = (value_obj or {}).get("volume")
        for of in offers:
            of_val = of.get("value")
            of_amt = _to_float(of.get("amount"))
            if isinstance(of_val, str) and of_val.strip().startswith("{") and of_val.strip().endswith("}"):
                try:
                    of_val = json.loads(of_val)
                except Exception:
                    try:
                        of_val = ast.literal_eval(of_val)
                    except Exception:
                        pass
            if isinstance(of_val, dict):
                if (vid is not None and of_val.get("id") == vid) or (vvol is not None and of_val.get("volume") == vvol):
                    return of_amt
            else:
                if raw_value is not None and of_val == raw_value:
                    return of_amt
    except Exception:
        pass
    return None


def _agent_level(value):
    level = str(value or "normal").strip().lower()
    return level if level in {"normal", "elite", "professional"} else "normal"


def _offers_for_agent_level(svc_doc, agent_level):
    base = svc_doc.get("offers") or []
    level_key = "elite_offers" if agent_level == "elite" else "professional_offers" if agent_level == "professional" else ""
    level_offers = svc_doc.get(level_key) if level_key else None
    if not level_offers:
        return base

    merged = []
    for idx, base_offer in enumerate(base):
        row = dict(base_offer or {})
        level_offer = level_offers[idx] if idx < len(level_offers) else {}
        if level_offer.get("amount") is not None:
            row["amount"] = level_offer.get("amount")
        merged.append(row)
    return merged


def _derive_base_profit(amount_total, base_amount_hint, eff_percent):
    a = _money(amount_total)
    if a <= 0:
        return 0.0, 0.0
    if base_amount_hint is not None and base_amount_hint > 0:
        base = float(base_amount_hint)
        profit = round(a - base, 2)
        if profit < 0:
            profit = 0.0
            base = a
        return round(base, 2), profit
    p = _to_float(eff_percent, 0.0) or 0.0
    try:
        base = round(a / (1.0 + (p / 100.0)), 2) if p > 0 else a
    except Exception:
        base = a
    profit = round(a - base, 2)
    if profit < 0:
        profit = 0.0
        base = a
    return base, profit


def _extract_provider_cost(payload):
    if not isinstance(payload, dict):
        return None

    data_block = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for src in (payload, data_block):
        for key in ("price", "cost", "package_amount", "provider_amount"):
            val = _to_float(src.get(key), None)
            if val is not None and val > 0:
                return round(float(val), 2)
    return None


def _profit_from_cost(amount_total, cost_amount):
    amount = _money(amount_total)
    cost = _to_float(cost_amount, None)
    if amount <= 0 or cost is None or cost <= 0:
        return None

    cost = round(float(cost), 2)
    profit = max(0.0, round(amount - cost, 2))
    percent = round((profit / cost) * 100.0, 2) if cost > 0 else 0.0
    return cost, profit, percent


# ===== Field resolvers =======================================================
def _resolve_network_id(item: dict, value_obj: dict, svc_doc: dict | None):
    """
    Internal numeric network ID, used only for duplicate guards / reporting.
    Not sent to providers.
    """
    nid = (item or {}).get("network_id") or (value_obj or {}).get("network_id")
    if nid not in (None, "", []):
        try:
            return int(nid)
        except Exception:
            pass
    if svc_doc:
        try:
            if "network_id" in svc_doc and svc_doc["network_id"] not in (None, ""):
                return int(svc_doc["network_id"])
            guess = (svc_doc.get("name") or svc_doc.get("network") or "").strip().upper()
            if guess and guess in NETWORK_ID_FALLBACK:
                return int(NETWORK_ID_FALLBACK[guess])
        except Exception:
            pass
    if not svc_doc:
        name = (item.get("serviceName") or "").strip().upper()
        if name in NETWORK_ID_FALLBACK:
            return int(NETWORK_ID_FALLBACK[name])
    return None


def _resolve_network_slug(svc_doc: dict | None, item: dict) -> str | None:
    """
    Resolve generic 'network' slug we also reuse:
      - 'mtn'
      - 'telecel'
      - 'airteltigo'
    Used for routing.
    """
    doc = svc_doc

    # Fallback: look up by service name if svc_doc is missing
    if not doc:
        sname = (item.get("serviceName") or "").strip()
        if sname:
            try:
                doc = services_col.find_one(
                    {"name": sname},
                    {"service_network": 1, "network": 1, "name": 1},
                )
            except Exception:
                doc = None

    candidates = []
    if doc:
        candidates.append(doc.get("service_network"))
        candidates.append(doc.get("network"))
        candidates.append(doc.get("name"))

    candidates.append(item.get("network"))
    candidates.append(item.get("network_name"))
    candidates.append(item.get("serviceName"))

    joined = " ".join(str(c) for c in candidates if c).lower()

    if "mtn" in joined:
        return "mtn"

    # Telecel / Vodafone rebrand
    if "telecel" in joined or "vodafone" in joined:
        return "telecel"

    # AirtelTigo / AT / iShare
    if (
        "airteltigo" in joined
        or "airtel tigo" in joined
        or "airtel-tigo" in joined
        or "at - ishare" in joined
        or "i share" in joined
        or "ishare" in joined
    ):
        return "airteltigo"

    return None


def _resolve_codecraft_network(svc_doc: dict | None, item: dict) -> str | None:
    candidates = [
        (svc_doc or {}).get("network"), (svc_doc or {}).get("service_network"),
        (svc_doc or {}).get("name"), (item or {}).get("network"),
        (item or {}).get("network_name"), (item or {}).get("serviceName"),
    ]
    joined = " ".join(str(c) for c in candidates if c).lower()
    if "mtn" in joined:
        return "MTN"
    if "telecel" in joined or "vodafone" in joined:
        return "TELECEL"
    if any(v in joined for v in ("airteltigo", "airtel tigo", "airtel-tigo", "ishare", "i share", "bigtime")) or re.search(r"\bat\b", joined):
        return "AT"
    return None


def _resolve_codecraft_gig(value_obj: dict, item: dict) -> int | None:
    value_obj = value_obj if isinstance(value_obj, dict) else (value_obj or {})
    volume = value_obj.get("volume")
    if volume not in (None, "", []):
        try:
            return max(1, int(round(float(volume) / 1000.0)))
        except Exception:
            pass
    match = re.search(r"(\d+(?:\.\d+)?)\s*gb", str((item or {}).get("value") or "").lower())
    if match:
        try:
            return int(float(match.group(1)))
        except Exception:
            pass
    return None


def _resolve_skplug_network(svc_doc: dict | None, item: dict) -> str | None:
    direct_candidates = [
        (svc_doc or {}).get("skplug_network"),
        (item or {}).get("skplug_network"),
        (item or {}).get("provider_network"),
        (svc_doc or {}).get("provider_network"),
    ]
    for raw in direct_candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        key = re.sub(r"[^a-z0-9]+", "", text.lower())
        if key == "mtn":
            return "MTN"
        if key in {"telecel", "vodafone"}:
            return "TELECEL"
        if key in {"atnoexpiry", "atnonexpiry", "atishare", "ishare"}:
            return "AT_NOEXPIRY"
        if key in {"atexpiry", "atbigtime", "bigtime"}:
            return "AT_EXPIRY"

    candidates = [
        (svc_doc or {}).get("network"),
        (svc_doc or {}).get("service_network"),
        (svc_doc or {}).get("name"),
        (item or {}).get("network"),
        (item or {}).get("network_name"),
        (item or {}).get("serviceName"),
    ]
    joined = " ".join(str(c) for c in candidates if c).lower()

    if "mtn" in joined:
        return "MTN"

    if "telecel" in joined or "vodafone" in joined:
        return "TELECEL"

    if "bigtime" in joined or "at expiry" in joined or "expiry" in joined:
        return "AT_EXPIRY"

    if (
        "airteltigo" in joined
        or "airtel tigo" in joined
        or "airtel-tigo" in joined
        or "ishare" in joined
        or "i share" in joined
        or "noexpiry" in joined
        or "no-expiry" in joined
        or re.search(r"\bat\b", joined)
    ):
        return "AT_NOEXPIRY"

    slug = _resolve_network_slug(svc_doc, item)
    return {
        "mtn": "MTN",
        "telecel": "TELECEL",
        "airteltigo": "AT_NOEXPIRY",
    }.get(slug)


def _resolve_package_size_gb(value_obj: dict, item: dict) -> int | None:
    """
    Resolve bundle size (integer GB) to use as Portal-02 "volume".
    """
    if not isinstance(value_obj, dict):
        value_obj = value_obj or {}

    # 1) explicit GB fields
    for key in ("gb", "gb_size", "package_size", "volume_gb", "size_gb"):
        val = value_obj.get(key)
        if val not in (None, "", []):
            try:
                return int(float(val))
            except Exception:
                pass

    # 2) 'volume' field (can be GB or MB)
    vol = value_obj.get("volume")
    if vol not in (None, "", []):
        try:
            vol_f = float(vol)
            if vol_f > 50:
                gb = max(1, round(vol_f / 1024.0))
            else:
                gb = vol_f
            return int(gb)
        except Exception:
            pass

    # 3) Parse from item['value'] string like '1GB', '5 GB'
    raw_val = item.get("value") or ""
    if isinstance(raw_val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return int(float(m.group(1)))
            except Exception:
                pass
        m2 = re.search(r"(\d+(?:\.\d+)?)", raw_val)
        if m2:
            try:
                return int(float(m2.group(1)))
            except Exception:
                pass

    return None


def _resolve_shared_bundle_mb(value_obj: dict, item: dict) -> int | None:
    """
    Resolve DataConnect shared_bundle (integer MB).
    Prefers value_obj["volume"] (already MB in MTN offers).
    Falls back to parsing item["value"] like '1GB'/'500MB'.
    """
    if not isinstance(value_obj, dict):
        value_obj = value_obj or {}

    vol = value_obj.get("volume")
    if vol not in (None, "", []):
        try:
            return int(float(vol))
        except Exception:
            pass

    raw_val = item.get("value") or ""
    if isinstance(raw_val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return int(float(m.group(1)) * 1000)
            except Exception:
                pass
        m2 = re.search(r"(\d+(?:\.\d+)?)\s*mb", raw_val.lower())
        if m2:
            try:
                return int(float(m2.group(1)))
            except Exception:
                pass
        m3 = re.search(r"(\d+(?:\.\d+)?)", raw_val)
        if m3:
            try:
                return int(float(m3.group(1)))
            except Exception:
                pass

    return None


def _resolve_datakazina_shared_bundle(value_obj: dict, item: dict) -> int | None:
    """
    Resolve DataKazina shared_bundle (package identifier).
    Prefers explicit package id from the MTN offer mapping, then falls back
    to parsing raw value text or converting MB->GB when an id is missing.
    """
    if not isinstance(value_obj, dict):
        value_obj = value_obj or {}

    for key in ("id", "package_id", "pkg_id", "bundle_id", "shared_bundle"):
        if value_obj.get(key) not in (None, "", []):
            try:
                return int(float(value_obj.get(key)))
            except Exception:
                pass

    raw_val = item.get("value") or item.get("label") or ""
    if isinstance(raw_val, str):
        txt = raw_val.strip()
        if txt.startswith("{") and txt.endswith("}"):
            try:
                parsed = json.loads(txt)
                if isinstance(parsed, dict) and parsed.get("id") not in (None, "", []):
                    return int(float(parsed.get("id")))
            except Exception:
                try:
                    parsed = ast.literal_eval(txt)
                    if isinstance(parsed, dict) and parsed.get("id") not in (None, "", []):
                        return int(float(parsed.get("id")))
                except Exception:
                    pass

        m = re.search(r"pkg\s*([0-9]+)", txt.lower())
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

        if re.fullmatch(r"\d+", txt):
            try:
                return int(txt)
            except Exception:
                pass

        m_gb = re.search(r"(\d+(?:\.\d+)?)\s*gb", txt.lower())
        if m_gb:
            try:
                return max(1, int(float(m_gb.group(1))))
            except Exception:
                pass

    vol = value_obj.get("volume")
    if vol not in (None, "", []):
        try:
            vol_mb = float(vol)
            if vol_mb >= 1000:
                gb = int(round(vol_mb / 1000.0))
                return max(1, gb)
            return int(vol_mb)
        except Exception:
            pass

    return None


def _build_bundle_key(value_obj: dict, item: dict):
    """
    Build a generic bundle key for duplicate detection.
    Returns ('bundle', <normalized_value>) or None.
    """
    val = None
    if isinstance(value_obj, dict):
        for key in ("id", "volume", "code", "package_size", "gb"):
            if value_obj.get(key) not in (None, "", []):
                val = value_obj.get(key)
                break
    if val is None:
        val = item.get("value") or item.get("label")

    if val is None:
        return None

    try:
        norm = int(float(val))
    except Exception:
        norm = str(val).strip()

    return ("bundle", norm)


def _normalize_msisdn_gh(phone: str) -> str:
    """
    Convert Ghana numbers to international format for Portal-02.
    """
    p = re.sub(r"\D", "", phone or "")
    if not p:
        return phone
    if p.startswith("0") and len(p) == 10:
        return "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return p


def _normalize_msisdn_gh_local(phone: str) -> str:
    p = re.sub(r"\D", "", phone or "")
    if not p:
        return phone
    if p.startswith("233") and len(p) == 12:
        return f"0{p[3:]}"
    if len(p) == 9:
        return f"0{p}"
    return p


def _normalize_phone_for_blocklist(phone: str) -> str:
    """
    Normalize to local format used by blocked_phone_numbers.normalized_phone.
    """
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def _known_number_local(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    if len(digits) > 10 and digits.startswith("0"):
        return digits[:10]
    return digits


def _known_number_candidates(phone: str) -> list[str]:
    raw = re.sub(r"\D+", "", str(phone or ""))
    local = _known_number_local(phone)
    candidates: list[str] = []
    for item in (local, raw):
        if item and item not in candidates:
            candidates.append(item)
    if local.startswith("0") and len(local) == 10:
        intl = "233" + local[1:]
        if intl not in candidates:
            candidates.append(intl)
    return candidates


def _service_requires_known_number_verification(item: dict, svc_doc: dict | None = None) -> bool:
    joined = " ".join(
        str(v or "").strip().lower()
        for v in (
            (item or {}).get("service_network"),
            (item or {}).get("network"),
            (item or {}).get("serviceName"),
            (item or {}).get("name"),
            (svc_doc or {}).get("service_network"),
            (svc_doc or {}).get("network"),
            (svc_doc or {}).get("name"),
        )
        if str(v or "").strip()
    )
    return "mtn" in joined


def _known_number_exists(phone: str) -> bool:
    candidates = _known_number_candidates(phone)
    if not candidates:
        return False
    try:
        allowed_override = known_number_attempts_col.find_one(
            {"normalized_phone": {"$in": candidates}, "is_allowed_override": True},
            {"_id": 1},
        )
    except Exception:
        allowed_override = None
    if allowed_override is not None:
        return True
    return orders_col.find_one({"items.phone": {"$in": candidates}}, {"_id": 1}) is not None


def _known_number_enforcement_enabled() -> bool:
    try:
        doc = settings_col.find_one({"_id": "checkout_controls"}, {"enforce_known_number_check": 1})
    except Exception:
        doc = None
    return bool((doc or {}).get("enforce_known_number_check", True))


def _record_not_in_database_number(phone: str, source: str = "", service_name: str = "") -> None:
    normalized_phone = _known_number_local(phone)
    if not normalized_phone:
        return

    now = datetime.utcnow()
    update = {
        "$set": {
            "phone": normalized_phone,
            "normalized_phone": normalized_phone,
            "last_seen_at": now,
            "updated_at": now,
        },
        "$inc": {"attempt_count": 1},
        "$setOnInsert": {"first_seen_at": now},
    }

    source = (source or "").strip().lower()
    if source:
        update.setdefault("$addToSet", {})["sources"] = source
        update["$set"]["last_source"] = source

    service_name = str(service_name or "").strip()
    if service_name:
        update["$set"]["last_service_name"] = service_name

    try:
        known_number_attempts_col.update_one(
            {"normalized_phone": normalized_phone},
            update,
            upsert=True,
        )
    except Exception:
        pass


def _known_number_validation_error(
    cart: list[dict],
    *,
    source: str = "",
    enforce_known_number: bool | None = None,
) -> dict | None:
    if not isinstance(cart, list):
        return None

    if enforce_known_number is None:
        enforce_known_number = _known_number_enforcement_enabled()

    verified_cache: dict[str, bool] = {}
    service_cache: dict[str, dict | None] = {}

    for idx, item in enumerate(cart, start=1):
        line = dict(item or {})
        service_id_raw = line.get("serviceId")
        svc_doc = None
        if service_id_raw:
            cache_key = str(service_id_raw)
            if cache_key not in service_cache:
                try:
                    service_cache[cache_key] = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {"name": 1, "network": 1, "service_network": 1},
                    )
                except Exception:
                    service_cache[cache_key] = None
            svc_doc = service_cache.get(cache_key)

        if not _service_requires_known_number_verification(line, svc_doc):
            continue

        local_phone = _known_number_local(line.get("phone") or "")
        if not local_phone:
            continue
        if local_phone not in verified_cache:
            verified_cache[local_phone] = _known_number_exists(local_phone)
        if verified_cache[local_phone]:
            continue

        _record_not_in_database_number(
            local_phone,
            source=source,
            service_name=(line.get("serviceName") or (svc_doc or {}).get("name") or ""),
        )

        if not enforce_known_number:
            continue

        return {
            "success": False,
            "message": "Number not in our database so you cant place order.",
            "phone": local_phone,
            "line_index": idx,
            "service_name": (
                line.get("serviceName")
                or (svc_doc or {}).get("name")
                or ""
            ),
        }

    return None


@checkout_bp.route("/api/verify-known-number", methods=["POST"])
def verify_known_number():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone") or ""
    service_stub = {
        "serviceId": data.get("serviceId"),
        "serviceName": data.get("serviceName"),
        "service_network": data.get("service_network"),
        "network": data.get("network"),
    }

    svc_doc = None
    service_id_raw = service_stub.get("serviceId")
    if service_id_raw:
        try:
            svc_doc = services_col.find_one(
                {"_id": ObjectId(service_id_raw)},
                {"name": 1, "network": 1, "service_network": 1},
            )
        except Exception:
            svc_doc = None

    normalized_phone = _known_number_local(phone)
    verification_required = _service_requires_known_number_verification(service_stub, svc_doc)
    enforce_known_number = _known_number_enforcement_enabled()
    if not verification_required:
        return jsonify(
            {
                "success": True,
                "verified": True,
                "allowed": True,
                "warning_only": False,
                "verification_required": False,
                "normalized_phone": normalized_phone,
                "message": "Verification not required for this service.",
            }
        ), 200

    if not re.fullmatch(r"0\d{9}", normalized_phone or ""):
        return jsonify(
            {
                "success": False,
                "verified": False,
                "allowed": False,
                "warning_only": False,
                "verification_required": True,
                "normalized_phone": normalized_phone,
                "message": "Phone must be 10 digits (e.g. 0530393625).",
            }
        ), 400

    verified = _known_number_exists(normalized_phone)
    source = (data.get("source") or "").strip().lower()
    service_name = service_stub.get("serviceName") or (svc_doc or {}).get("name") or ""
    if not verified:
        _record_not_in_database_number(normalized_phone, source=source, service_name=service_name)

    warning_only = bool((not verified) and (not enforce_known_number))
    message = (
        "Number verified."
        if verified
        else "Delivery May Take 24 hrs or more to deliver for this number."
        if warning_only
        else "Number not in our database so you cant place order."
    )
    return jsonify(
        {
            "success": verified or warning_only,
            "verified": verified,
            "allowed": verified or warning_only,
            "warning_only": warning_only,
            "enforce_known_number": enforce_known_number,
            "verification_required": True,
            "normalized_phone": normalized_phone,
            "message": message,
        }
    ), (200 if (verified or warning_only) else 404)


def _resolve_portal02_offer_slug(svc_doc: dict | None, item: dict) -> str:
    """
    Decide which offerSlug to send to Portal-02.
    """
    if item.get("offerSlug"):
        return str(item["offerSlug"])

    if svc_doc:
        if svc_doc.get("portal02_offer_slug"):
            return str(svc_doc["portal02_offer_slug"])
        if svc_doc.get("offerSlug"):
            return str(svc_doc["offerSlug"])

        nm = str(svc_doc.get("name", "")).lower()
        net = str(svc_doc.get("network", "")).lower()
        combo = f"{nm} {net}"

        if "telecel" in combo:
            return PORTAL02_OFFER_SLUG_TELECEL

        if (
            "ishare" in combo
            or "i share" in combo
            or "at - ishare" in combo
            or ("airtel" in combo and "tigo" in combo)
        ):
            return PORTAL02_OFFER_SLUG_ISHARE

    return PORTAL02_OFFER_SLUG_MTN_NORMAL


# ===== Provider callers (used by background worker) ==========================
def _send_portal02_order(phone: str, network: str, volume_gb: int,
                         offer_slug: str,
                         external_ref: str, order_id: str, debug_events: list):
    if not PORTAL02_API_KEY or PORTAL02_API_KEY == "dk_your_api_key_here":
        err = {
            "success": False,
            "error": "PORTAL02 API key not configured",
            "type": "CONFIG_ERROR",
            "http_status": 500,
        }
        jlog("portal02_config_error", order_id=order_id, ref=external_ref)
        return False, err

    url = f"{PORTAL02_BASE_URL.rstrip('/')}/order/{network}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": PORTAL02_API_KEY,
    }
    body = {
        "type": "single",
        "volume": int(volume_gb),
        "phone": phone,
        "offerSlug": offer_slug,
        "webhookUrl": PORTAL02_WEBHOOK_URL,
    }

    masked = phone[:5] + "***" + phone[-2:] if phone and len(phone) >= 7 else "***"
    jlog(
        "portal02_request_body",
        order_id=order_id,
        ref=external_ref,
        body={
            "network": network,
            "phone": masked,
            "volume": body["volume"],
            "offerSlug": body["offerSlug"],
        },
    )

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=45,
        )
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            resp.status_code in (200, 201)
            and isinstance(payload, dict)
            and bool(payload.get("success")) is True
        )
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)

        dbg = {
            "status": resp.status_code,
            "body_len": len(text),
        }
        jlog("portal02_response", order_id=order_id, ref=external_ref, payload=payload)
        jlog("portal02_call", order_id=order_id, ref=external_ref, ok=ok, debug=dbg)
        debug_events.append(
            {
                "when": datetime.utcnow(),
                "stage": "portal02-place-order",
                "ok": ok,
                "http_status": resp.status_code,
            }
        )
        return ok, payload

    except requests.RequestException as e:
        jlog("portal02_network_error", order_id=order_id, ref=external_ref, error=str(e))
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}


def _send_skplug_order(
    phone: str,
    network: str,
    gb_size: int,
    external_ref: str,
    order_id: str,
    debug_events: list,
):
    token = (SKPLUG_API_KEY or "").strip()
    if not token:
        err = {
            "success": False,
            "error": "SKPLUG API key not configured",
            "type": "CONFIG_ERROR",
            "http_status": 500,
        }
        jlog("skplug_config_error", order_id=order_id, ref=external_ref)
        return False, err

    url = f"{SKPLUG_BASE_URL.rstrip('/')}/order/"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = {
        "recipient": str(_normalize_msisdn_gh_local(phone)),
        "network": str(network),
        "gb_size": str(int(gb_size)),
    }

    masked = phone[:5] + "***" + phone[-2:] if phone and len(phone) >= 7 else "***"
    jlog(
        "skplug_request_body",
        order_id=order_id,
        ref=external_ref,
        body={
            "recipient": masked,
            "network": body["network"],
            "gb_size": body["gb_size"],
        },
    )

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=SKPLUG_TIMEOUT,
        )
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = 200 <= resp.status_code < 300
        if isinstance(payload, dict):
            status_text = str(payload.get("status") or "").strip().lower()
            if payload.get("success") is False or status_text in {"failed", "failure", "error"}:
                ok = False
            payload.setdefault("http_status", resp.status_code)

        jlog("skplug_response", order_id=order_id, ref=external_ref, payload=payload)
        jlog("skplug_call", order_id=order_id, ref=external_ref, ok=ok, status=resp.status_code)
        debug_events.append(
            {
                "when": datetime.utcnow(),
                "stage": "skplug-place-order",
                "ok": ok,
                "http_status": resp.status_code,
            }
        )
        return ok, payload

    except requests.RequestException as e:
        jlog("skplug_network_error", order_id=order_id, ref=external_ref, error=str(e))
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}


# ===== DataConnect Provider caller ===========================================
def _send_dataconnect_order(
    phone: str,
    network_id: int,
    shared_bundle: int,
    external_ref: str,
    order_id: str,
    debug_events: list,
):
    if not DATACONNECT_API_KEY:
        err = {
            "success": False,
            "error": "DATACONNECT API key not configured",
            "type": "CONFIG_ERROR",
            "http_status": 500,
        }
        jlog("dataconnect_config_error", order_id=order_id, ref=external_ref)
        return False, err

    url = f"{DATACONNECT_BASE_URL.rstrip('/')}/buy-other-package"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": DATACONNECT_API_KEY,
    }
    body = {
        "recipient_msisdn": str(phone),
        "network_id": int(network_id),
        "shared_bundle": int(shared_bundle),
    }

    masked = phone[:5] + "***" + phone[-2:] if phone and len(phone) >= 7 else "***"
    jlog(
        "dataconnect_request_body",
        order_id=order_id,
        ref=external_ref,
        body={
            "recipient_msisdn": masked,
            "network_id": body["network_id"],
            "shared_bundle": body["shared_bundle"],
        },
    )

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=45,
        )
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            resp.status_code in (200, 201)
            and isinstance(payload, dict)
            and bool(payload.get("success")) is True
        )
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)

        dbg = {"status": resp.status_code, "body_len": len(text)}
        jlog("dataconnect_response", order_id=order_id, ref=external_ref, payload=payload)
        jlog("dataconnect_call", order_id=order_id, ref=external_ref, ok=ok, debug=dbg)
        debug_events.append(
            {
                "when": datetime.utcnow(),
                "stage": "dataconnect-buy-other-package",
                "ok": ok,
                "http_status": resp.status_code,
            }
        )
        return ok, payload

    except requests.RequestException as e:
        jlog("dataconnect_network_error", order_id=order_id, ref=external_ref, error=str(e))
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}


# ===== DataKazina Provider caller ============================================
def _datakazina_submit_single(
    recipient_msisdn: str,
    shared_bundle: int,
    incoming_api_ref: str,
    order_id: str | None = None,
    debug_events: list | None = None,
    meta: dict | None = None,
):
    key = _clean_api_key(DATAKAZINA_API_KEY)
    if not key:
        err = {
            "success": False,
            "error": "DATAKAZINA API key not configured",
            "type": "CONFIG_ERROR",
            "http_status": 500,
        }
        jlog("datakazina_config_error", order_id=order_id, ref=incoming_api_ref)
        return {
            "ok": False,
            "http_status": 500,
            "provider": "datakazina",
            "provider_reference": None,
            "response": err,
            "message": err.get("error"),
        }

    url = f"{DATAKAZINA_BASE_URL.rstrip('/')}/buy-data-package"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": key,
    }
    body = {
        "recipient_msisdn": str(recipient_msisdn),
        "network_id": 3,
        "shared_bundle": int(shared_bundle),
        "incoming_api_ref": str(incoming_api_ref),
    }

    masked = recipient_msisdn[:5] + "***" + recipient_msisdn[-2:] if recipient_msisdn and len(recipient_msisdn) >= 7 else "***"
    jlog(
        "datakazina_request_prepared",
        order_id=order_id,
        ref=incoming_api_ref,
        body={
            "recipient_msisdn": masked,
            "network_id": body["network_id"],
            "shared_bundle": body["shared_bundle"],
        },
        meta=meta or {},
    )

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=DATAKAZINA_TIMEOUT)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            200 <= resp.status_code < 300
            and isinstance(payload, dict)
            and bool(payload.get("success")) is True
        )
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)

        provider_ref = payload.get("transaction_code") if isinstance(payload, dict) else None
        msg = payload.get("message") if isinstance(payload, dict) else None

        jlog("datakazina_response", order_id=order_id, ref=incoming_api_ref, payload=payload)
        if debug_events is not None:
            debug_events.append(
                {
                    "when": datetime.utcnow(),
                    "stage": "datakazina-buy-data-package",
                    "ok": ok,
                    "http_status": resp.status_code,
                }
            )

        return {
            "ok": ok,
            "http_status": int(resp.status_code),
            "provider": "datakazina",
            "provider_reference": provider_ref,
            "response": payload,
            "message": msg or (payload.get("error") if isinstance(payload, dict) else None) or "",
        }

    except requests.RequestException as e:
        jlog("datakazina_error", order_id=order_id, ref=incoming_api_ref, error=str(e))
        err = {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}
        return {
            "ok": False,
            "http_status": 599,
            "provider": "datakazina",
            "provider_reference": None,
            "response": err,
            "message": str(e),
        }


def _datakazina_submit_many_as_single_orders(jobs: list[dict]):
    """
    Process multiple DataKazina jobs as sequential single POSTs.
    """
    results = []
    success_count = 0
    failed_count = 0

    for job in jobs or []:
        resp = _datakazina_submit_single(
            recipient_msisdn=job.get("recipient_msisdn") or job.get("phone"),
            shared_bundle=job.get("shared_bundle"),
            incoming_api_ref=job.get("incoming_api_ref") or job.get("provider_request_order_id") or "",
            order_id=job.get("order_id"),
            debug_events=job.get("debug_events"),
            meta=job.get("meta"),
        )
        results.append(resp)
        if resp.get("ok"):
            success_count += 1
        else:
            failed_count += 1

    return {
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def _codecraft_get_packages_cached():
    now = time.time()
    if _CODECRAFT_CACHE["ts"] and now - _CODECRAFT_CACHE["ts"] < CODECRAFT_TTL_SEC:
        return _CODECRAFT_CACHE["packages"]
    if not CODECRAFT_API_KEY:
        jlog("codecraft_config_error", reason="missing_api_key")
        return {}
    try:
        response = requests.get(
            f"{CODECRAFT_BASE_URL.rstrip('/')}/packages.php",
            headers={"Accept": "application/json", "x-api-key": CODECRAFT_API_KEY}, timeout=30,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text or ""}
        if response.status_code not in (200, 201):
            jlog("codecraft_packages_http_error", status=response.status_code)
            return {}
        data = payload.get("data") if isinstance(payload, dict) else {}
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        package_map = {}
        for row in ((data or {}).get("regular_packages") or []) + ((data or {}).get("bigtime_packages") or []):
            if not isinstance(row, dict):
                continue
            try:
                key = (str(row.get("network") or "").upper(), int(float(row.get("package"))))
                package_map.setdefault(key, float(row.get("amount")))
            except Exception:
                continue
        _CODECRAFT_CACHE.update(ts=now, packages=package_map)
        jlog("codecraft_packages_loaded", packages=len(package_map), http_status=response.status_code)
        return package_map
    except requests.RequestException as exc:
        jlog("codecraft_packages_error", error=str(exc))
        return {}
    except Exception as exc:
        jlog("codecraft_packages_parse_error", error=str(exc))
        return {}


def _codecraft_submit(phone, gig, network, external_ref, order_id, debug_events):
    if not CODECRAFT_API_KEY:
        return False, {"success": False, "error": "CODECRAFT API key not configured", "http_status": 500}, None
    body = {"recipient_number": str(phone), "gig": str(gig), "network": str(network)}
    try:
        response = requests.post(
            f"{CODECRAFT_BASE_URL.rstrip('/')}/initiate.php",
            headers={"Accept": "application/json", "Content-Type": "application/json", "x-api-key": CODECRAFT_API_KEY},
            json=body, timeout=45,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {"raw": response.text or ""}
        reference = payload.get("reference_id") if isinstance(payload, dict) else None
        message = str(payload.get("message") or "").lower() if isinstance(payload, dict) else ""
        ok = bool(reference) and (payload.get("status") == 200 or payload.get("success") is True or "recorded" in message)
        if isinstance(payload, dict):
            payload.setdefault("http_status", response.status_code)
        debug_events.append({"when": datetime.utcnow(), "stage": "codecraft-initiate", "ok": ok, "http_status": response.status_code})
        jlog("codecraft_call", order_id=order_id, ref=external_ref, ok=ok, status=response.status_code)
        return ok, payload, reference
    except requests.RequestException as exc:
        jlog("codecraft_network_error", order_id=order_id, ref=external_ref, error=str(exc))
        return False, {"success": False, "error": str(exc), "type": "NETWORK_ERROR", "http_status": 599}, None


# ===== Unavailability checker ================================================
def _service_unavailability_reason(svc_doc: dict):
    """
    Returns (is_unavailable, reason_text)
    """
    if not svc_doc:
        return True, "Closed"

    status = (svc_doc.get("status") or "").strip().upper()
    availability = (svc_doc.get("availability") or "").strip().upper()
    display = (svc_doc.get("display") or "ON").strip().upper()

    if display == "OFF":
        return True, "Service is not available"

    if availability in {"OUT_OF_STOCK", "OUT OF STOCK", "OUTOFSTOCK"}:
        return True, "Out of stock"

    if status == "CLOSED":
        return True, "Closed"

    return False, ""


# ===== Duplicate-in-processing guard =========================================
DUP_WINDOW_MINUTES = 30


def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0


def _has_processing_conflict_strict(
    phone: str,
    service_id_raw: str | None,
    svc_name: str | None,
    network_id: int | None,
    bundle_key: tuple | None,
    amount_key: float,
) -> bool:
    if not phone or network_id is None or bundle_key is None:
        return False

    window_start = datetime.utcnow() - timedelta(minutes=DUP_WINDOW_MINUTES)
    kind, bval = bundle_key

    elem = {
        "phone": phone,
        "network_id": network_id,
        "bundle_key.kind": kind,
        "bundle_key.value": bval,
        "amount": amount_key,
    }
    if service_id_raw:
        elem["serviceId"] = service_id_raw

    q = {
        "status": {"$in": ["processing", "Pending", "pending"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": elem},
    }
    if orders_col.find_one(q, {"_id": 1}):
        return True

    alt = {
        "phone": phone,
        "network_id": network_id,
        "amount": amount_key,
    }
    if kind == "offer":
        alt["value_obj.id"] = bval
    else:
        alt["value_obj.volume"] = bval
    if service_id_raw:
        alt["serviceId"] = service_id_raw

    q2 = {
        "status": {"$in": ["processing", "Pending", "pending"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": alt},
    }
    return bool(orders_col.find_one(q2, {"_id": 1}))


def _send_bundleportal_order(
    phone: str,
    network: str,
    package_size_gb: int,
    external_ref: str,
    order_id: str,
    debug_events: list,
):
    """Place an idempotent BundlePortal order using our per-line reference."""
    key = _clean_api_key(BUNDLEPORTAL_API_KEY)
    if not key:
        err = {
            "success": False,
            "error": "BUNDLEPORTAL API key not configured",
            "type": "CONFIG_ERROR",
            "http_status": 500,
        }
        jlog("bundleportal_config_error", order_id=order_id, ref=external_ref)
        return False, err

    normalized_phone = _normalize_msisdn_gh_local(phone)
    network_slug = str(network or "").strip().lower()
    if network_slug == "ishare":
        network_slug = "airteltigo"

    body = {
        "action": "place_order",
        "network": network_slug,
        "recipient": normalized_phone,
        "package_size": int(package_size_gb),
        "order_id": str(external_ref)[:80],
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": key,
    }
    masked = normalized_phone[:5] + "***" + normalized_phone[-2:] if len(normalized_phone) >= 7 else "***"
    jlog(
        "bundleportal_request_body",
        order_id=order_id,
        ref=external_ref,
        body={**body, "recipient": masked},
    )

    try:
        resp = requests.post(
            BUNDLEPORTAL_BASE_URL.rstrip("/"),
            headers=headers,
            json=body,
            timeout=BUNDLEPORTAL_TIMEOUT,
        )
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}
        ok = 200 <= resp.status_code < 300 and isinstance(payload, dict) and payload.get("success") is True
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        jlog("bundleportal_response", order_id=order_id, ref=external_ref, ok=ok, payload=payload)
        debug_events.append({
            "when": datetime.utcnow(),
            "stage": "bundleportal-place-order",
            "ok": ok,
            "http_status": resp.status_code,
        })
        return ok, payload
    except requests.RequestException as exc:
        jlog("bundleportal_network_error", order_id=order_id, ref=external_ref, error=str(exc))
        return False, {
            "success": False,
            "error": str(exc),
            "type": "NETWORK_ERROR",
            "http_status": 599,
        }


# ===== BACKGROUND WORKER =====================================================
def _background_process_providers(order_id: str, api_jobs: list[dict]):
    """
    Runs in a separate thread AFTER the HTTP response is sent.
    It picks queued lines and calls Portal-02, then updates the order doc.
    """
    try:
        gate_doc = orders_col.find_one(
            {"order_id": order_id},
            {"channel": 1, "source": 1, "payment_status": 1, "status": 1},
        )
        source = gate_doc.get("source") if isinstance(gate_doc, dict) else {}
        is_ussd = (
            (gate_doc or {}).get("channel") == "arkesel_ussd"
            or (isinstance(source, dict) and source.get("provider") == "arkesel")
        )
        if is_ussd and str((gate_doc or {}).get("payment_status") or "").strip().lower() != "paid":
            jlog(
                "checkout_bg_worker_blocked_unpaid_ussd",
                order_id=order_id,
                payment_status=(gate_doc or {}).get("payment_status"),
                status=(gate_doc or {}).get("status"),
            )
            return
    except Exception as exc:
        jlog("checkout_bg_worker_payment_gate_error", order_id=order_id, error=str(exc))
        return

    jlog("checkout_bg_worker_start", order_id=order_id, jobs=len(api_jobs))
    local_debug = []

    for job in api_jobs:
        try:
            line_ref = job["provider_request_order_id"]
            phone = job["phone"]
            package_size_gb = job.get("package_size_gb")
            provider = job["provider"]
            portal_network_slug = job.get("portal02_network_slug")
            network_id = job.get("network_id")
            shared_bundle = job.get("shared_bundle")
            svc_id = job.get("service_id")
            provider_network = job.get("provider_network")
            provider_gig = job.get("provider_gig")
            provider_amount = job.get("provider_amount")
            line_amount = _money(job.get("amount"))

            svc_doc = None
            if svc_id:
                try:
                    svc_doc = services_col.find_one(
                        {"_id": svc_id},
                        {
                            "type": 1,
                            "provider": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "offers": 1,
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "status": 1,
                            "availability": 1,
                            "display": 1,
                            "service_network": 1,
                            "portal02_offer_slug": 1,
                            "offerSlug": 1,
                        },
                    )
                except Exception:
                    svc_doc = None

            ok = False
            payload = {}
            provider_ref = None

            if provider == "portal02":
                offer_slug = _resolve_portal02_offer_slug(svc_doc or {}, job.get("raw_item") or {})
                normalized_phone = _normalize_msisdn_gh(phone)
                ok, payload = _send_portal02_order(
                    phone=normalized_phone,
                    network=portal_network_slug,
                    volume_gb=package_size_gb,
                    offer_slug=offer_slug,
                    external_ref=line_ref,
                    order_id=order_id,
                    debug_events=local_debug,
                )
            elif provider == "dataconnect":
                ok, payload = _send_dataconnect_order(
                    phone=phone,
                    network_id=network_id,
                    shared_bundle=shared_bundle,
                    external_ref=line_ref,
                    order_id=order_id,
                    debug_events=local_debug,
                )
            elif provider == "skplug":
                ok, payload = _send_skplug_order(
                    phone=phone,
                    network=provider_network,
                    gb_size=package_size_gb,
                    external_ref=line_ref,
                    order_id=order_id,
                    debug_events=local_debug,
                )
            elif provider == "datakazina":
                resp = _datakazina_submit_single(
                    recipient_msisdn=phone,
                    shared_bundle=shared_bundle,
                    incoming_api_ref=line_ref,
                    order_id=order_id,
                    debug_events=local_debug,
                    meta={"line_index": job.get("line_index")},
                )
                ok = bool(resp.get("ok"))
                payload = resp.get("response") if isinstance(resp, dict) else {}
                provider_ref = resp.get("provider_reference") if isinstance(resp, dict) else None
            elif provider == "codecraft":
                ok, payload, provider_ref = _codecraft_submit(
                    phone=phone, gig=provider_gig, network=provider_network,
                    external_ref=line_ref, order_id=order_id, debug_events=local_debug,
                )
            elif provider == "bundleportal":
                ok, payload = _send_bundleportal_order(
                    phone=phone,
                    network=provider_network,
                    package_size_gb=package_size_gb,
                    external_ref=line_ref,
                    order_id=order_id,
                    debug_events=local_debug,
                )

            provider_order_id = None
            if provider == "codecraft" and isinstance(payload, dict):
                provider_ref = payload.get("reference_id") or provider_ref
                provider_order_id = provider_ref
            elif provider == "bundleportal" and isinstance(payload, dict):
                data_block = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                provider_ref = data_block.get("reference")
                provider_order_id = data_block.get("order_id") or line_ref
            elif provider == "skplug" and isinstance(payload, dict):
                data_block = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                provider_ref = (
                    payload.get("reference")
                    or payload.get("order_reference")
                    or payload.get("transaction_code")
                    or data_block.get("reference")
                    or data_block.get("order_reference")
                    or data_block.get("transaction_code")
                )
                provider_order_id = (
                    payload.get("orderId")
                    or payload.get("order_id")
                    or payload.get("id")
                    or data_block.get("orderId")
                    or data_block.get("order_id")
                    or data_block.get("id")
                )
            elif isinstance(payload, dict):
                provider_ref = (
                    payload.get("transaction_code")
                    or payload.get("reference")
                    or payload.get("order_reference")
                )
                provider_order_id = (
                    payload.get("orderId")
                    or payload.get("order_id")
                    or payload.get("transaction_code")
                )

            api_status_val = "success" if ok else "failed"

            update_fields = {
                "items.$.provider": provider,
                "items.$.api_status": api_status_val,
                "items.$.api_response": payload,
                "items.$.provider_reference": provider_ref,
                "items.$.provider_order_id": provider_order_id,
                "updated_at": datetime.utcnow(),
            }
            if provider == "dataconnect":
                update_fields["items.$.line_status"] = "processing" if ok else "failed"
            if provider == "datakazina":
                update_fields["items.$.line_status"] = "processing" if ok else "failed"
            if provider == "skplug":
                update_fields["items.$.line_status"] = "processing" if ok else "failed"
                update_fields["items.$.provider_network"] = provider_network
                provider_cost = _extract_provider_cost(payload) if ok else None
                profit_parts = _profit_from_cost(line_amount, provider_cost)
                if profit_parts:
                    cost, profit_amount, profit_percent = profit_parts
                    update_fields["items.$.base_amount"] = cost
                    update_fields["items.$.profit_amount"] = profit_amount
                    update_fields["items.$.profit_percent_used"] = profit_percent
                    update_fields["items.$.provider_package_amount"] = cost
            if provider == "codecraft":
                update_fields["items.$.line_status"] = "processing" if ok else "failed"
                update_fields["items.$.provider_network"] = provider_network
                update_fields["items.$.provider_gig"] = provider_gig
                update_fields["items.$.provider_package_amount"] = provider_amount
                profit_parts = _profit_from_cost(line_amount, provider_amount) if ok else None
                if profit_parts:
                    cost, profit_amount, profit_percent = profit_parts
                    update_fields["items.$.base_amount"] = cost
                    update_fields["items.$.profit_amount"] = profit_amount
                    update_fields["items.$.profit_percent_used"] = profit_percent
            if provider == "bundleportal":
                update_fields["items.$.line_status"] = "processing" if ok else "failed"
                update_fields["items.$.provider_network"] = provider_network
                data_block = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
                provider_cost = _to_float(data_block.get("amount")) if ok else None
                provider_state = str(data_block.get("status") or "").strip().lower()
                update_fields["items.$.provider_status"] = provider_state or None
                profit_parts = _profit_from_cost(line_amount, provider_cost)
                if profit_parts:
                    cost, profit_amount, profit_percent = profit_parts
                    update_fields["items.$.base_amount"] = cost
                    update_fields["items.$.profit_amount"] = profit_amount
                    update_fields["items.$.profit_percent_used"] = profit_percent
                    update_fields["items.$.provider_package_amount"] = cost

            # Update this specific line inside the order items
            orders_col.update_one(
                {
                    "order_id": order_id,
                    "items.provider_request_order_id": line_ref,
                },
                {
                    "$set": update_fields
                },
            )
            try:
                refreshed = orders_col.find_one({"order_id": order_id}, {"items.profit_amount": 1})
                if refreshed and isinstance(refreshed.get("items"), list):
                    total_profit_now = round(
                        sum(_money(it.get("profit_amount")) for it in refreshed["items"]),
                        2,
                    )
                    orders_col.update_one(
                        {"order_id": order_id},
                        {"$set": {"profit_amount_total": total_profit_now, "updated_at": datetime.utcnow()}},
                    )
            except Exception as e:
                jlog("checkout_profit_total_refresh_error", order_id=order_id, error=str(e))
        except Exception as e:
            # Never leave a persisted provider job in its initial queued state.
            # This is especially important for synchronous store checkout: the
            # buyer must receive a real failed result when submission crashes.
            line_ref = job.get("provider_request_order_id") if isinstance(job, dict) else None
            if line_ref:
                orders_col.update_one(
                    {
                        "order_id": order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "failed",
                            "items.$.line_status": "failed",
                            "items.$.api_response": {
                                "success": False,
                                "error": str(e),
                                "type": "PROVIDER_SUBMISSION_ERROR",
                            },
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
            jlog("checkout_bg_worker_line_error", order_id=order_id, error=str(e))

    # Update root order status if failures occurred
    try:
        order_doc = orders_col.find_one({"order_id": order_id}, {"items.api_status": 1, "items.line_status": 1, "status": 1})
        if order_doc and isinstance(order_doc.get("items"), list):
            statuses = []
            for it in order_doc["items"]:
                st = (it.get("api_status") or "").lower()
                ls = (it.get("line_status") or "").lower()
                statuses.append("failed" if (st == "failed" or ls == "failed") else "ok")

            if statuses:
                any_failed = any(s == "failed" for s in statuses)
                all_failed = all(s == "failed" for s in statuses)
                if any_failed:
                    new_status = "failed" if all_failed else "processing"
                    orders_col.update_one(
                        {"order_id": order_id},
                        {"$set": {"status": new_status, "updated_at": datetime.utcnow()}},
                    )
    except Exception:
        pass

    if local_debug:
        # append debug entries
        try:
            orders_col.update_one(
                {"order_id": order_id},
                {"$push": {"debug.events": {"$each": local_debug}}},
            )
        except Exception:
            pass

    jlog("checkout_bg_worker_end", order_id=order_id, jobs=len(api_jobs))


# ===== Route (FAST RESPONSE, PROVIDERS IN BACKGROUND) ========================
@checkout_bp.route("/checkout", methods=["POST"])
def process_checkout():
    try:
        # Auth
        if "user_id" not in session or session.get("role") != "customer":
            jlog("checkout_auth_fail", session_keys=list(session.keys()))
            return jsonify({"success": False, "message": "Not authorized"}), 401

        try:
            user_id = ObjectId(session["user_id"])
        except Exception:
            return jsonify({"success": False, "message": "Invalid user ID"}), 400

        data = request.get_json(silent=True) or {}
        cart = data.get("cart", [])
        method = data.get("method", "wallet")
        jlog("checkout_incoming", payload=data)

        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        user_doc = users_col.find_one({"_id": user_id}, {"agent_level": 1}) or {}
        agent_level = _agent_level(user_doc.get("agent_level"))

        # Reprice from the customer's agent-level manual offers on the server.
        # The browser can display prices, but the database remains the source of truth.
        server_cart = []
        for item in cart:
            line = dict(item or {})
            service_id_raw = line.get("serviceId")
            value_obj = _coerce_value_obj(line.get("value_obj") or line.get("value"))
            svc_doc = None
            if service_id_raw:
                try:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {"offers": 1, "elite_offers": 1, "professional_offers": 1, "unit": 1, "name": 1, "display": 1}
                    )
                except Exception:
                    svc_doc = None
            if svc_doc:
                offer_ladder = _offers_for_agent_level(svc_doc, agent_level)
                agent_price = _pick_offer_base_amount_from_service(
                    {**svc_doc, "offers": offer_ladder},
                    value_obj,
                    line.get("value")
                )
                base_price = _pick_offer_base_amount_from_service(
                    svc_doc,
                    value_obj,
                    line.get("value")
                )
            else:
                agent_price = None
                base_price = None
            if agent_price is not None and agent_price > 0:
                line["amount"] = round(float(agent_price), 2)
                line["total"] = round(float(agent_price), 2)
                if base_price is not None and base_price > 0:
                    line["base_amount"] = round(float(base_price), 2)
                else:
                    line["base_amount"] = round(float(agent_price), 2)
            server_cart.append(line)
        cart = server_cart

        known_number_error = _known_number_validation_error(cart, source="customer_dashboard")
        if known_number_error:
            return jsonify(known_number_error), 400

        # Total requested (customer-facing)
        total_requested = sum(_money(item.get("amount")) for item in cart)
        if total_requested <= 0:
            return jsonify({"success": False, "message": "Total amount must be greater than zero"}), 400

        order_id = generate_order_id()

        # Balance check
        bal_doc = balances_col.find_one({"user_id": user_id}) or {}
        current_balance = _money(bal_doc.get("amount", 0))
        jlog("checkout_balance", order_id=order_id, balance=current_balance, total=total_requested)
        if current_balance < total_requested:
            return jsonify({"success": False, "message": "❌ Insufficient wallet balance"}), 400

        results = []
        debug_events = []

        total_delivered_api_amount = 0.0  # stays 0.0 (we don't mark delivered immediately)
        total_processing_amount = 0.0
        api_requested_total = 0.0
        has_processing = False
        profit_amount_total = 0.0

        seen_keys = set()
        api_jobs = []  # lines to be sent to providers in the background worker
        blocked_phone_map = {}
        try:
            cart_phone_keys = {
                _normalize_phone_for_blocklist((it.get("phone") or "").strip())
                for it in cart
                if (it.get("phone") or "").strip()
            }
            cart_phone_keys.discard("")
            if cart_phone_keys:
                blocked_docs = blocked_phone_numbers_col.find(
                    {"is_active": True, "normalized_phone": {"$in": list(cart_phone_keys)}},
                    {"normalized_phone": 1, "reason": 1, "_id": 0},
                )
                blocked_phone_map = {
                    d.get("normalized_phone"): (d.get("reason") or "")
                    for d in blocked_docs
                    if d.get("normalized_phone")
                }
        except Exception as e:
            jlog("blocked_phone_lookup_error", order_id=order_id, error=str(e))
            blocked_phone_map = {}

        for idx, item in enumerate(cart, start=1):
            phone = (item.get("phone") or "").strip()
            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
            amt_total = _money(item.get("amount"))
            amount_key = _normalize_amount_key(amt_total)

            service_id_raw = item.get("serviceId")
            svc_doc = None
            svc_type = None
            svc_name = item.get("serviceName") or None

            if service_id_raw:
                try:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {
                            "type": 1,
                            "provider": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "offers": 1,
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "status": 1,
                            "availability": 1,
                            "display": 1,
                            "service_network": 1,
                            "portal02_offer_slug": 1,
                            "offerSlug": 1,
                        },
                    )
                    if svc_doc:
                        st = svc_doc.get("type")
                        svc_type = (st.strip().upper() if isinstance(st, str) else st)
                        svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
                except Exception:
                    svc_doc = None
                    svc_type = None

            # HARD GATE: availability
            is_unavail, reason_text = _service_unavailability_reason(svc_doc)
            if is_unavail:
                return jsonify(
                    {
                        "success": False,
                        "message": reason_text,
                        "unavailable": {
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "reason": reason_text,
                        },
                    }
                ), 400

            network_id = _resolve_network_id(item, value_obj, svc_doc)
            bundle_key = _build_bundle_key(value_obj, item)

            normalized_phone = _normalize_phone_for_blocklist(phone)
            blocked_reason = blocked_phone_map.get(normalized_phone, "")
            if normalized_phone and normalized_phone in blocked_phone_map:
                base_hint = _to_float(item.get("base_amount"))
                base_amount = round(float(base_hint if base_hint is not None else 0.0), 2)
                profit_amount = max(0.0, round(amt_total - base_amount, 2))
                profit_percent_used = round((profit_amount / base_amount) * 100.0, 2) if base_amount > 0 else 0.0
                jlog(
                    "checkout_blocked_phone_manual",
                    order_id=order_id,
                    idx=idx,
                    normalized_phone=normalized_phone,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                )

                has_processing = True
                total_processing_amount += amt_total
                profit_amount_total += profit_amount
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "blocked_phone_manual",
                        "api_response": {
                            "note": "Phone number is blocked from API processing; queued for manual processing.",
                            "blocked_reason": blocked_reason,
                            "normalized_phone": normalized_phone,
                        },
                        "provider": "manual",
                        "is_blocked_phone": True,
                    }
                )
                continue

            # Duplicate guards
            if phone and (network_id is not None) and (bundle_key is not None):
                cart_key = (phone, int(network_id), bundle_key[1], bundle_key[0], amount_key)
                if cart_key in seen_keys:
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": 0.0,
                            "amount": 0.0,
                            "originally_requested_amount": amt_total,
                            "profit_amount": 0.0,
                            "profit_percent_used": 0.0,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                            "network_id": network_id,
                            "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]},
                            "line_amount_key": amount_key,
                            "line_status": "skipped_duplicate_in_cart",
                            "api_status": "skipped",
                            "api_response": {
                                "note": "Duplicate line in this cart (same number, network, bundle, amount)"
                            },
                        }
                    )
                    continue
                seen_keys.add(cart_key)

            is_dup_strict = _has_processing_conflict_strict(
                phone, service_id_raw, svc_name, network_id, bundle_key, amount_key
            )
            if is_dup_strict:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_processing",
                        "api_status": "skipped",
                        "api_response": {
                            "note": "Same number + same network + same bundle + same amount already processing; skipping."
                        },
                    }
                )
                continue

            # base & profit (requested): profit = amount - base_amount
            base_hint = _to_float(item.get("base_amount"))
            base_amount = round(float(base_hint if base_hint is not None else 0.0), 2)
            profit_amount = max(0.0, round(amt_total - base_amount, 2))
            profit_percent_used = round((profit_amount / base_amount) * 100.0, 2) if base_amount > 0 else 0.0
            profit_amount_total += profit_amount

            # No service doc → manual processing
            if not svc_doc:
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else "unknown",
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable",
                        "api_response": {"note": "Service not found; queued for processing"},
                    }
                )
                continue

            # Provider selection
            resolved_network = _resolve_network_slug(svc_doc, item)
            svc_name_norm = (svc_name or "").strip().lower()
            svc_network_norm = (svc_doc.get("network") or "").strip().lower() if svc_doc else ""
            combo_name_net = f"{svc_name_norm} {svc_network_norm}"

            is_mtn_express = (svc_name_norm == "mtn express")
            is_mtn_normal = (svc_name_norm == "mtn normal")
            is_telecel_bundle = ("telecel" in combo_name_net)
            is_ishare_bundle = (
                "ishare" in combo_name_net
                or "i share" in combo_name_net
                or "at - ishare" in combo_name_net
            )

            svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
            type_allows_api = svc_type_flag in ("ON", "API")
            api_allowed = type_allows_api or is_telecel_bundle or is_ishare_bundle
            if svc_type_flag == "OFF":
                api_allowed = False

            provider_raw = svc_doc.get("provider") if svc_doc else None
            svc_provider_from_db = str(provider_raw).strip().lower() if provider_raw is not None else ""
            if svc_provider_from_db not in ("portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"):
                jlog(
                    "provider_defaulted_to_portal02",
                    order_id=order_id,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider_raw=provider_raw,
                    reason="missing_or_invalid",
                )
                svc_provider_from_db = "portal02"

            if svc_provider_from_db == "dataconnect" and not (is_mtn_express or is_mtn_normal):
                jlog(
                    "provider_defaulted_to_portal02",
                    order_id=order_id,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider_raw=provider_raw,
                    reason="non_mtn_service_for_dataconnect",
                )
                svc_provider_from_db = "portal02"

            if svc_provider_from_db == "datakazina" and not (is_mtn_express or is_mtn_normal):
                jlog(
                    "provider_defaulted_to_portal02",
                    order_id=order_id,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider_raw=provider_raw,
                    reason="non_mtn_service_for_datakazina",
                )
                svc_provider_from_db = "portal02"

            use_codecraft = svc_provider_from_db == "codecraft" and api_allowed
            use_datakazina = (
                (is_mtn_express or is_mtn_normal)
                and svc_provider_from_db == "datakazina"
                and api_allowed
            )

            use_dataconnect = (
                (is_mtn_express or is_mtn_normal)
                and svc_provider_from_db == "dataconnect"
                and api_allowed
            )
            use_skplug = api_allowed and svc_provider_from_db == "skplug"
            skplug_network = _resolve_skplug_network(svc_doc, item) if use_skplug else None
            use_bundleportal = api_allowed and svc_provider_from_db == "bundleportal"

            portal02_network_slug = None
            if api_allowed and svc_provider_from_db == "portal02":
                if resolved_network in ("mtn", "telecel", "airteltigo"):
                    portal02_network_slug = resolved_network

            use_portal02 = portal02_network_slug is not None

            jlog(
                "checkout_line_routing",
                order_id=order_id,
                idx=idx,
                serviceId=service_id_raw,
                serviceName=svc_name,
                resolved_network=resolved_network,
                api_allowed=api_allowed,
                svc_provider_from_db=svc_provider_from_db,
                datakazina_selected=use_datakazina,
                selected_provider=(
                    "codecraft" if use_codecraft else "bundleportal" if use_bundleportal else "datakazina" if use_datakazina else "dataconnect" if use_dataconnect else "skplug" if use_skplug else "portal02" if use_portal02 else "manual"
                ),
            )

            if use_codecraft:
                network = _resolve_codecraft_network(svc_doc, item)
                gig = _resolve_codecraft_gig(value_obj, item)
                package_map = _codecraft_get_packages_cached()
                provider_amount = package_map.get((network, gig)) if network and gig else None
                if not phone or not network or not gig or provider_amount is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append({
                        "phone": phone, "base_amount": base_amount, "amount": amt_total,
                        "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                        "value": item.get("value"), "value_obj": value_obj,
                        "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key, "line_status": "processing",
                        "api_status": "skipped_package_not_found" if network and gig else "skipped_missing_fields",
                        "api_response": {"note": "CodeCraft fields or package missing; queued for manual processing.", "network": network, "gig": gig},
                        "provider": "codecraft",
                    })
                    continue
                api_requested_total += amt_total
                external_ref = f"{order_id}{idx}{uuid.uuid4().hex[:6]}"
                has_processing = True
                total_processing_amount += amt_total
                results.append({
                    "phone": phone, "base_amount": base_amount, "amount": amt_total,
                    "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                    "value": item.get("value"), "value_obj": value_obj,
                    "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                    "provider": "codecraft", "provider_network": network, "provider_gig": gig,
                    "provider_package_amount": provider_amount, "provider_reference": None,
                    "provider_order_id": None, "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key, "line_status": "processing", "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to CodeCraft"},
                })
                api_jobs.append({
                    "provider_request_order_id": external_ref, "phone": phone, "amount": amt_total,
                    "provider": "codecraft", "provider_network": network, "provider_gig": gig,
                    "provider_amount": provider_amount, "service_id": svc_doc["_id"], "line_index": idx,
                })
                continue

            if use_bundleportal:
                package_size_gb = _resolve_package_size_gb(value_obj, item)
                bp_network = resolved_network if resolved_network in ("mtn", "telecel", "airteltigo") else None
                if not phone or not bp_network or package_size_gb is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append({
                        "phone": phone, "base_amount": base_amount, "amount": amt_total,
                        "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                        "value": item.get("value"), "value_obj": value_obj,
                        "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key, "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {"note": "BundlePortal fields missing; queued for manual processing."},
                        "provider": "bundleportal",
                    })
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
                api_requested_total += amt_total
                has_processing = True
                total_processing_amount += amt_total
                results.append({
                    "phone": phone, "base_amount": base_amount, "amount": amt_total,
                    "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                    "value": item.get("value"), "value_obj": value_obj,
                    "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                    "provider": "bundleportal", "provider_network": bp_network,
                    "provider_reference": None, "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "package_size_gb": package_size_gb, "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key, "line_status": "processing", "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to BundlePortal"},
                })
                api_jobs.append({
                    "provider_request_order_id": external_ref, "phone": phone, "amount": amt_total,
                    "provider": "bundleportal", "provider_network": bp_network,
                    "package_size_gb": package_size_gb,
                    "service_id": svc_doc["_id"] if svc_doc else None, "line_index": idx,
                })
                continue

            if use_datakazina:
                shared_bundle = _resolve_datakazina_shared_bundle(value_obj, item)
                dk_network_id = 3

                if not phone or not shared_bundle:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": dk_network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "DataKazina fields missing; queued for processing",
                                "got": {"phone": bool(phone), "shared_bundle": shared_bundle},
                            },
                            "provider": "datakazina",
                        }
                    )
                    continue

                api_requested_total += amt_total

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "datakazina",
                    "provider_network": "mtn",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": dk_network_id,
                    "shared_bundle": int(shared_bundle),
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to DataKazina"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "network_id": dk_network_id,
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                jlog(
                    "datakazina_job_prepared",
                    order_id=order_id,
                    idx=idx,
                    ref=external_ref,
                    serviceId=service_id_raw,
                    network_id=dk_network_id,
                    shared_bundle=int(shared_bundle),
                )
                continue

            if use_dataconnect:
                shared_bundle = _resolve_shared_bundle_mb(value_obj, item)
                dc_network_id = _resolve_network_id(item, value_obj, svc_doc)
                if dc_network_id is None and resolved_network == "mtn":
                    dc_network_id = 3
                try:
                    dc_network_id = int(dc_network_id) if dc_network_id is not None else None
                except Exception:
                    dc_network_id = None

                if not phone or not shared_bundle or not dc_network_id:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "DataConnect fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "network_id": dc_network_id,
                                    "shared_bundle": shared_bundle,
                                },
                            },
                            "provider": "dataconnect",
                        }
                    )
                    continue

                api_requested_total += amt_total

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "dataconnect",
                    "provider_network": "mtn",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": dc_network_id,
                    "shared_bundle": int(shared_bundle),
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to DataConnect"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "dataconnect",
                    "network_id": int(dc_network_id),
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_skplug:
                package_size_gb = _resolve_package_size_gb(value_obj, item)

                if not phone or not skplug_network or package_size_gb is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "SKPlug fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "network": skplug_network,
                                    "package_size_gb": package_size_gb,
                                },
                            },
                            "provider": "skplug",
                        }
                    )
                    continue

                api_requested_total += amt_total
                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "skplug",
                    "provider_network": skplug_network,
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to SKPlug"},
                }

                results.append(line_record)

                api_jobs.append(
                    {
                        "provider_request_order_id": external_ref,
                        "phone": phone,
                        "amount": amt_total,
                        "provider": "skplug",
                        "provider_network": skplug_network,
                        "package_size_gb": package_size_gb,
                        "service_id": svc_doc["_id"] if svc_doc else None,
                        "line_index": idx,
                    }
                )
                continue

            if not use_portal02:
                has_processing = True
                total_processing_amount += amt_total

                if not api_allowed:
                    note = (
                        "API calls disabled for this service (type OFF and not a mapped Telecel/iShare); "
                        "queued for manual processing."
                    )
                    api_status = "not_applicable_type_off"
                else:
                    note = (
                        "API is handled via Portal-02, but this line did not match any mapped network; "
                        "queued for manual processing."
                    )
                    api_status = "not_applicable_network"

                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": api_status,
                        "api_response": {
                            "note": note,
                            "resolved_network": resolved_network,
                            "serviceName": svc_name,
                            "service_type_flag": svc_type_flag,
                        },
                    }
                )
                continue

            # From here: API-eligible line → we will send it via BACKGROUND worker
            api_requested_total += amt_total

            package_size_gb = _resolve_package_size_gb(value_obj, item)

            if not phone or package_size_gb is None:
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {
                            "note": "API fields missing; queued for processing",
                            "got": {
                                "phone": bool(phone),
                                "resolved_network": resolved_network,
                                "package_size_gb": package_size_gb,
                            },
                        },
                    }
                )
                continue

            # Prepare background job meta
            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

            provider_name = "portal02"
            provider_network_slug = portal02_network_slug

            has_processing = True
            total_processing_amount += amt_total

            # store line with "queued" status; background worker will update
            line_record = {
                "phone": phone,
                "base_amount": base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "provider": provider_name,
                "provider_network": provider_network_slug,
                "provider_reference": None,
                "provider_order_id": None,
                "provider_request_order_id": external_ref,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key,
                "line_status": "processing",
                "api_status": "submitting",
                "api_response": {"note": "Submitting directly to provider"},
            }

            results.append(line_record)

            job_payload = {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": provider_name,
                "portal02_network_slug": portal02_network_slug,
                "package_size_gb": package_size_gb,
                "service_id": svc_doc["_id"],
                "raw_item": item,
            }

            api_jobs.append(job_payload)

        if len(debug_events) > 10:
            debug_events = debug_events[-10:]

        total_to_charge_now = round(total_delivered_api_amount + total_processing_amount, 2)

        # Ensure consistent provider fields on all line items
        for it in results:
            it.setdefault("provider", "manual")
            it.setdefault("provider_request_order_id", None)
            it.setdefault("provider_reference", None)
            it.setdefault("provider_order_id", None)
            it.setdefault("api_status", it.get("api_status") or "not_applicable")
            it.setdefault("api_response", it.get("api_response") or {})

        # If nothing to charge (all skipped)
        if total_to_charge_now <= 0:
            for it in (results or []):
                if not it.get("line_status"):
                    it["line_status"] = "pending"
                if isinstance(it.get("value"), (dict, list)):
                    it["value"] = ""
                if not it.get("value"):
                    vo = it.get("value_obj") or {}
                    vol = vo.get("volume")
                    if isinstance(vol, (int, float)) and vol > 0:
                        gb = vol / 1000
                        it["value"] = f"{gb:g}GB"
                    else:
                        it["value"] = "N/A"
            created_docs, _order_jobs = _persist_split_order_docs(
                orders_collection=orders_col,
                results=results,
                base_order_fields={
                    "user_id": user_id,
                    "order_id": order_id,
                    "status": "skipped",
                    "paid_from": method,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                    "debug": {"events": debug_events},
                },
            )
            primary_order_id = created_docs[0]["order_id"] if created_docs else order_id
            _clear_customer_cart(user_id)
            skipped_count = sum(
                1
                for it in results
                if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
            )
            return (
                jsonify(
                    {
                        "success": True,
                        "message": (
                            "No charge taken. {n} item(s) were skipped because the same phone, network, bundle, "
                            "and amount already has an order in processing or duplicated in cart."
                        ).format(n=skipped_count),
                        "order_id": primary_order_id,
                        "order_ids": [doc.get("order_id") for doc in created_docs],
                        "redirect_url": f"/invoice/{primary_order_id}",
                        "status": "skipped",
                        "charged_amount": 0.0,
                        "profit_amount_total": 0.0,
                        "skipped_count": skipped_count,
                        "items": results,
                    }
                ),
                200,
            )

        # Deduct balance NOW
        balances_col.update_one(
            {"user_id": user_id},
            {"$inc": {"amount": -total_to_charge_now}, "$set": {"updated_at": datetime.utcnow()}},
            upsert=True,
        )

        status = "pending"

        for it in (results or []):
            if not it.get("line_status"):
                it["line_status"] = "pending"
            if isinstance(it.get("value"), (dict, list)):
                it["value"] = ""
            if not it.get("value"):
                vo = it.get("value_obj") or {}
                vol = vo.get("volume")
                if isinstance(vol, (int, float)) and vol > 0:
                    gb = vol / 1000
                    it["value"] = f"{gb:g}GB"
                else:
                    it["value"] = "N/A"

        created_docs, order_jobs = _persist_split_order_docs(
            orders_collection=orders_col,
            results=results,
            base_order_fields={
                "user_id": user_id,
                "order_id": order_id,
                "status": status,
                "paid_from": method,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "debug": {"events": debug_events},
            },
            api_jobs=api_jobs,
        )
        primary_order_id = created_docs[0]["order_id"] if created_docs else order_id
        _clear_customer_cart(user_id)

        # Record transaction
        transactions_col.insert_one(
            {
                "user_id": user_id,
                "amount": total_to_charge_now,
                "reference": primary_order_id,
                "status": "success",
                "type": "purchase",
                "gateway": "Wallet",
                "currency": "GHS",
                "created_at": datetime.utcnow(),
                "verified_at": datetime.utcnow(),
                "meta": {
                    "order_status": status,
                    "api_delivered_amount": round(total_delivered_api_amount, 2),
                    "processing_amount": round(total_processing_amount, 2),
                    "profit_amount_total": round(profit_amount_total, 2),
                },
            }
        )

        skipped_count = sum(
            1
            for it in results
            if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )
        processing_count = sum(1 for it in results if it.get("line_status") == "processing")

        # 🔥 Spawn background worker for provider calls (does not block response)
        if order_jobs:
            try:
                for split_order_id, split_jobs in order_jobs:
                    _background_process_providers(split_order_id, split_jobs)
                refreshed_docs = [orders_col.find_one({"order_id": doc.get("order_id")}) or doc for doc in created_docs]
                created_docs = refreshed_docs
                primary_order_doc = refreshed_docs[0] if refreshed_docs else primary_order_doc
                results = [item for doc in refreshed_docs for item in (doc.get("items") or [])]
                status = str(primary_order_doc.get("status") or status)
            except Exception as e:
                jlog("checkout_provider_submit_error", order_id=primary_order_id, error=str(e))

        msg = (
            "📝 Order received and is processing. "
            "We’ve charged your wallet. Order ID: {oid}"
        ).format(oid=primary_order_id)

        return (
            jsonify(
                {
                    "success": True,
                    "message": msg,
                    "order_id": primary_order_id,
                    "order_ids": [doc.get("order_id") for doc in created_docs],
                    "redirect_url": f"/invoice/{primary_order_id}",  # frontend already uses this
                    "status": status,
                    "charged_amount": total_to_charge_now,
                    "profit_amount_total": round(profit_amount_total, 2),
                    "processing_count": processing_count,
                    "skipped_count": skipped_count,
                    "items": results,
                }
            ),
            200,
        )

    except Exception:
        jlog("checkout_uncaught", error=traceback.format_exc())
        return jsonify({"success": False, "message": "Server error"}), 500


# ===== Invoice view (same blueprint) =========================================
@checkout_bp.route("/invoice/<order_id>")
def invoice_view(order_id):
    """
    Render a single invoice by Nagonu Order ID (e.g. NAN12345)
    Uses invoice.html template you already created.
    """
    order = orders_col.find_one({"order_id": order_id})
    if not order:
        abort(404)

    user = {}
    try:
        uid = order.get("user_id")
        if uid:
            user = users_col.find_one({"_id": uid}) or {}
    except Exception:
        user = {}

    customer_name = (
        user.get("name")
        or user.get("full_name")
        or user.get("username")
        or "Customer"
    )

    return render_template(
        "invoice.html",
        order=order,
        user=user,
        customer=customer_name,
    )
