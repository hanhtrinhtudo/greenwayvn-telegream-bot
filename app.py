import os
import json
import re
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ============== OpenAI (tùy chọn, để hiểu intent & mượt câu trả lời) ==============
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ============== ENV ==============
load_dotenv()

TELEGRAM_TOKEN        = os.getenv("TELEGRAM_TOKEN", "")
HOTLINE_TUYEN_TREN    = os.getenv("HOTLINE_TUYEN_TREN", "09xx.xxx.xxx")
LINK_KENH_TELEGRAM    = os.getenv("LINK_KENH_TELEGRAM", "https://t.me/...")
LINK_FANPAGE          = os.getenv("LINK_FANPAGE", "https://facebook.com/...")
LINK_WEBSITE          = os.getenv("LINK_WEBSITE", "https://...")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
LOG_SHEET_WEBHOOK_URL = os.getenv("LOG_SHEET_WEBHOOK_URL", "")

# Chat id nhóm / leader tuyến trên để forward yêu cầu hỗ trợ
UPLINE_CHAT_ID        = os.getenv("UPLINE_CHAT_ID", "")  # ví dụ: "-1001234567890"

ENABLE_AI_POLISH      = os.getenv("ENABLE_AI_POLISH", "true").lower() == "true"

# Lưu trạng thái: TVV vừa bấm "Kết nối tuyến trên" và đang chuẩn bị gửi câu hỏi
ESCALATION_PENDING: dict[int, bool] = {}  # {chat_id: True}

if not TELEGRAM_TOKEN:
    raise RuntimeError("Chưa cấu hình TELEGRAM_TOKEN trong .env")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ============== OpenAI client (nếu có) ==============
client = None
if OPENAI_API_KEY and OpenAI is not None:
    client = OpenAI(api_key=OPENAI_API_KEY)

# ============== Load data (products.json + combos.json) ==============
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")

with open(os.path.join(DATA_DIR, "products.json"), "r", encoding="utf-8") as f:
    PRODUCTS_DATA = json.load(f)
with open(os.path.join(DATA_DIR, "combos.json"), "r", encoding="utf-8") as f:
    COMBOS_DATA = json.load(f)

# Chấp nhận format {"products":[...]} hoặc list thẳng
PRODUCTS = PRODUCTS_DATA.get("products", PRODUCTS_DATA)
COMBOS   = COMBOS_DATA.get("combos", COMBOS_DATA)

# Load thêm metadata health_tags + triệu chứng + alias bổ sung (nếu có)
try:
    with open(os.path.join(DATA_DIR, "health_tags_info.json"), "r", encoding="utf-8") as f:
        HEALTH_TAGS_INFO = json.load(f)
except FileNotFoundError:
    HEALTH_TAGS_INFO = {}

try:
    with open(os.path.join(DATA_DIR, "symptoms_map.json"), "r", encoding="utf-8") as f:
        SYMPTOMS_MAP_RAW = json.load(f)
except FileNotFoundError:
    SYMPTOMS_MAP_RAW = {}

try:
    with open(os.path.join(DATA_DIR, "product_aliases.json"), "r", encoding="utf-8") as f:
        PRODUCT_ALIASES_DATA = json.load(f)
        PRODUCT_ALIASES_BY_ALIAS = PRODUCT_ALIASES_DATA.get("by_alias", PRODUCT_ALIASES_DATA)
except FileNotFoundError:
    PRODUCT_ALIASES_BY_ALIAS = {}

HEALTH_TAG_LABELS = {
    "tieu_duong": "hỗ trợ ổn định đường huyết, tiểu đường",
    "tieu_hoa": "hỗ trợ tiêu hóa, đường ruột",
    "gan": "hỗ trợ chức năng gan, thải độc gan",
    "thai_doc": "thải độc, giải độc cơ thể",
    "mien_dich": "tăng cường hệ miễn dịch",
    "tim_mach": "hỗ trợ tim mạch, huyết áp",
    "xuong_khop": "hỗ trợ xương khớp, giảm đau khớp",
    "than": "hỗ trợ thận – tiết niệu",
    "ung_thu": "hỗ trợ bệnh lý/u bướu, ung thư (kết hợp phác đồ)",
    "giam_mo": "giảm mỡ, kiểm soát cân nặng",
}
# Bổ sung/ghi đè nhãn từ file health_tags_info.json (nếu có)
if "HEALTH_TAGS_INFO" in globals() and HEALTH_TAGS_INFO:
    for _tag, _info in HEALTH_TAGS_INFO.items():
        _lbl = (_info.get("label") or "").strip()
        if _lbl:
            HEALTH_TAG_LABELS[_tag] = _lbl

def build_usecase_from_tags(tags):
    labels = []
    for t in tags or []:
        lbl = HEALTH_TAG_LABELS.get(t)
        if lbl and lbl not in labels:
            labels.append(lbl)
    return "; ".join(labels)

# ---------- Helper chuẩn hóa & health tags ----------

def normalize_for_match(s: str) -> str:
    """Lower + bỏ dấu + bỏ ký tự lạ để so khớp alias/keyword."""
    import unicodedata
    if not s:
        return ""
    s = str(s).lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Map keyword → health_tag (không phụ thuộc dữ liệu, anh có thể bổ sung dần)
_HEALTH_KEYWORD_TO_TAG_RAW = {
    "tiểu đường": "tieu_duong",
    "dai thao duong": "tieu_duong",
    "đái tháo đường": "tieu_duong",
    "duong huyet": "tieu_duong",
    "đường huyết": "tieu_duong",

    "da day": "da_day",
    "dạ dày": "da_day",
    "bao tu": "da_day",
    "bao tử": "da_day",
    "trao nguoc": "da_day",
    "trào ngược": "da_day",
    "o chua": "da_day",
    "ợ chua": "da_day",

    "tieu hoa": "tieu_hoa",
    "tiêu hóa": "tieu_hoa",
    "tieu hoá": "tieu_hoa",
    "tao bon": "tieu_hoa",
    "táo bón": "tieu_hoa",

    "gan": "gan",
    "men gan": "gan",
    "gan nhiem mo": "gan",
    "gan nhiễm mỡ": "gan",

    "xuong khop": "xuong_khop",
    "xương khớp": "xuong_khop",
    "dau khop": "xuong_khop",
    "đau khớp": "xuong_khop",
    "gout": "xuong_khop",

    "huyet ap": "tim_mach",
    "huyết áp": "tim_mach",
    "tim mach": "tim_mach",
    "tim mạch": "tim_mach",

    "thai doc": "thai_doc",
    "thải độc": "thai_doc",
    "detox": "thai_doc",

    "ung thu": "ung_thu",
    "ung thư": "ung_thu",
}

# Chuẩn hóa keyword map
HEALTH_KEYWORD_TO_TAG = {
    normalize_for_match(k): v for k, v in _HEALTH_KEYWORD_TO_TAG_RAW.items()
}

# Chuẩn hóa symptoms_map từ file JSON: key (triệu chứng) → list health_tags
SYMPTOMS_MAP_NORM = {}
if "SYMPTOMS_MAP_RAW" in globals() and isinstance(SYMPTOMS_MAP_RAW, dict):
    for raw_symptom, info in SYMPTOMS_MAP_RAW.items():
        tags = []
        if isinstance(info, dict):
            tags = info.get("health_tags", []) or []
        elif isinstance(info, list):
            tags = info
        key_norm = normalize_for_match(raw_symptom)
        if key_norm and tags:
            SYMPTOMS_MAP_NORM[key_norm] = tags

def extract_health_tags_from_text(text: str):
    """Trích health_tags từ câu mô tả triệu chứng/bệnh lý.

    Ưu tiên:
    1) Map từ symptoms_map.json (triệu chứng → nhiều health_tags)
    2) Fallback: map từ keyword tĩnh HEALTH_KEYWORD_TO_TAG
    """
    nt = normalize_for_match(text)
    tags: set[str] = set()

    # 1) Theo triệu chứng trong file JSON
    if "SYMPTOMS_MAP_NORM" in globals():
        for sym_norm, tags_list in SYMPTOMS_MAP_NORM.items():
            if sym_norm and sym_norm in nt:
                for t in tags_list:
                    if t:
                        tags.add(t)

    # 2) Theo keyword map cứng (bổ sung)
    for kw_norm, tag in HEALTH_KEYWORD_TO_TAG.items():
        if kw_norm and kw_norm in nt:
            tags.add(tag)

    return tags


def build_product_aliases(p: dict):
    """Sinh thêm alias từ name + code + aliases gốc."""
    aliases = set()
    name = p.get("name", "")
    code = str(p.get("code", "")).lstrip("#").strip()
    if code:
        p["code"] = code

    if name:
        aliases.add(name)
        aliases.add(name.lower())

        # tách theo ngoặc, dấu /, -, …
        for part in re.findall(r"[\w\u00C0-\u017F\-\/]+", name):
            aliases.add(part)

    for a in p.get("aliases", []):
        if a:
            aliases.add(a)

    if code:
        aliases.add(code)

    # chuẩn hóa khoảng trắng
    aliases_clean = []
    for a in aliases:
        a2 = re.sub(r"\s+", " ", str(a)).strip()
        if a2:
            aliases_clean.append(a2)

    p["aliases"] = aliases_clean

def build_combo_aliases(c: dict):
    aliases = set()
    name = c.get("name", "")
    if name:
        aliases.add(name)
        aliases.add(name.lower())
        for part in re.findall(r"[\w\u00C0-\u017F\-\/]+", name):
            aliases.add(part)
    for a in c.get("aliases", []):
        if a:
            aliases.add(a)
    aliases_clean = []
    for a in aliases:
        a2 = re.sub(r"\s+", " ", str(a)).strip()
        if a2:
            aliases_clean.append(a2)
    c["aliases"] = aliases_clean

# ---------- Build PRODUCTS + alias index + health_tags ----------

PRODUCT_MAP: dict[str, dict] = {}
PRODUCT_ALIAS_INDEX: dict[str, set[str]] = {}   # alias_norm → set(code)

for p in PRODUCTS:
    build_product_aliases(p)
    code = p.get("code")
    if not code:
        continue

    # Gắn health_tags (kết hợp tag có sẵn trong JSON + detect từ text)
    current_tags = set(p.get("health_tags", []))
    text_for_tags = " ".join([
        p.get("name", ""),
        p.get("benefits_text", "") or p.get("benefits", "") or "",
        p.get("ingredients_text", "") or p.get("ingredients", "") or "",
        p.get("usage_text", "") or p.get("usage", "") or "",
    ])
    auto_tags = extract_health_tags_from_text(text_for_tags)
    all_tags = sorted(current_tags.union(auto_tags))
    if all_tags:
        p["health_tags"] = all_tags

    PRODUCT_MAP[code] = p

    for a in p["aliases"]:
        na = normalize_for_match(a)
        if not na:
            continue
        PRODUCT_ALIAS_INDEX.setdefault(na, set()).add(code)

# Bổ sung alias từ file product_aliases.json (nếu có)
if "PRODUCT_ALIASES_BY_ALIAS" in globals() and PRODUCT_ALIASES_BY_ALIAS:
    for alias_norm, codes in PRODUCT_ALIASES_BY_ALIAS.items():
        na = normalize_for_match(alias_norm)
        if not na:
            continue
        for code in codes:
            if not code:
                continue
            PRODUCT_ALIAS_INDEX.setdefault(na, set()).add(str(code))

# ---------- Build COMBOS + alias index + health_tags ----------

COMBO_ID_MAP: dict[str, dict] = {}
COMBO_ALIAS_INDEX: dict[str, list[dict]] = {}   # alias_norm → [combo]

for c in COMBOS:
    build_combo_aliases(c)
    cid = c.get("id") or normalize_for_match(c.get("name", "") or "")
    c["id"] = cid

    # health_tags combo = tag từ JSON + tag từ tên/description + tag của từng product trong combo
    combo_tags = set(c.get("health_tags", []))
    text_for_tags = " ".join([
        c.get("name", ""),
        c.get("header_text", ""),
        c.get("duration_text", ""),
    ])
    combo_tags |= extract_health_tags_from_text(text_for_tags)

    for item in c.get("products", []):
        code = str(item.get("product_code", "")).lstrip("#").strip()
        item["product_code"] = code
        p = PRODUCT_MAP.get(code)
        if p:
            # đẩy name/url/price từ product sang nếu thiếu
            item.setdefault("name", p.get("name", ""))
            item.setdefault("price_text", p.get("price_text", ""))
            item.setdefault("product_url", p.get("product_url", ""))
            for t in p.get("health_tags", []):
                combo_tags.add(t)

    if combo_tags:
        c["health_tags"] = sorted(combo_tags)

    COMBO_ID_MAP[cid] = c

    for a in c["aliases"]:
        na = normalize_for_match(a)
        if not na:
            continue
        COMBO_ALIAS_INDEX.setdefault(na, []).append(c)

# ============== Telegram Keyboard ==============
MAIN_KEYBOARD = {
    "keyboard": [
        [
            {"text": "🧩 Combo theo vấn đề sức khỏe"},
            {"text": "🔎 Tra cứu sản phẩm"}
        ],
        [
            {"text": "🛒 Hướng dẫn mua hàng"},
            {"text": "☎️ Kết nối tuyến trên"}
        ],
        [
            {"text": "📢 Kênh & Fanpage"}
        ]
    ],
    "resize_keyboard": True,
    "one_time_keyboard": False
}

# ============== Flask app ==============
app = Flask(__name__)

# ============== Helper: gửi message Telegram ==============
def send_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)

    url = f"{TELEGRAM_API}/sendMessage"
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Error sending message:", e)

# ============== Helper: utility ==============
def contains_any(text, keywords):
    text = text.lower()
    return any(k.lower() in text for k in keywords)

def extract_code(text: str):
    """
    Tự động bắt mã sản phẩm dạng 6 chữ số (VD: 070728, 01590…).
    Có thể tùy chỉnh regex nếu cần.
    """
    text = text.strip()
    codes = re.findall(r"\b0\d{4,5}\b", text)
    return codes[0] if codes else None

def find_best_products(text: str, limit: int = 5):
    """
    Tìm sản phẩm theo alias (name, mã, alias mở rộng).
    """
    t = normalize_for_match(text)
    results = []
    seen = set()

    # match alias_norm nằm trong text_norm
    for alias_norm, codes in PRODUCT_ALIAS_INDEX.items():
        if alias_norm and alias_norm in t:
            for code in codes:
                if code not in seen and code in PRODUCT_MAP:
                    seen.add(code)
                    results.append(PRODUCT_MAP[code])
                    if len(results) >= limit:
                        return results

    # fallback: match theo từng token
    if not results:
        tokens = t.split()
        for alias_norm, codes in PRODUCT_ALIAS_INDEX.items():
            if any(tok in alias_norm for tok in tokens):
                for code in codes:
                    if code not in seen and code in PRODUCT_MAP:
                        seen.add(code)
                        results.append(PRODUCT_MAP[code])
                        if len(results) >= limit:
                            return results

    return results

def find_products_by_health(text: str, limit: int = 5):
    """
    Tìm sản phẩm theo health_tags (từ JSON) + từ khóa trong câu.
    """
    tags_from_text = extract_health_tags_from_text(text)
    results = []
    seen = set()

    # Ưu tiên match theo health_tags
    if tags_from_text:
        for p in PRODUCTS:
            p_tags = set(p.get("health_tags", []))
            if p_tags.intersection(tags_from_text):
                code = p.get("code")
                if code and code not in seen:
                    seen.add(code)
                    results.append(p)
                    if len(results) >= limit:
                        break

    # Nếu chưa có gì → fallback theo alias
    if not results:
        results = find_best_products(text, limit=limit)

    return results

def find_best_combo(text: str, limit: int = 3):
    """
    Match combo theo alias (tên, alias) – dùng cho fallback.
    """
    t = normalize_for_match(text)
    results = []
    seen = set()

    for alias_norm, combos in COMBO_ALIAS_INDEX.items():
        if alias_norm and alias_norm in t:
            for c in combos:
                cid = c.get("id")
                if cid not in seen:
                    seen.add(cid)
                    results.append(c)
                    if len(results) >= limit:
                        return results
    return results

def find_combo_by_health_keyword(text: str) -> dict | None:
    """
    Tìm combo theo health_tags + alias.
    """
    tags_from_text = extract_health_tags_from_text(text)
    best = None
    score_best = 0
    text_norm = normalize_for_match(text)

    # Ưu tiên match theo health_tags
    for c in COMBOS:
        c_tags = set(c.get("health_tags", []))
        score = len(c_tags.intersection(tags_from_text)) if tags_from_text else 0
        # cộng thêm điểm nếu alias trùng trong text
        for a in c.get("aliases", []):
            if normalize_for_match(a) in text_norm:
                score += 1
        if score > score_best:
            score_best = score
            best = c

    # Nếu chưa ra, fallback thuần theo alias
    if not best:
        combos = find_best_combo(text, limit=1)
        best = combos[0] if combos else None

    return best

# ============== AI: phân loại intent ==============
INTENT_LABELS = [
    "start",
    "buy_payment",
    "business_escalation",
    "business_escalation_detail",  # dùng cho log khi TVV gửi nội dung nhờ tuyến trên
    "channels",
    "combo_health",
    "product_info",
    "product_by_code",
    "health_products",
    "menu_combo",
    "menu_product_search",
    "menu_buy_payment",
    "menu_business_escalation",
    "menu_channels",
    "fallback"
]

def classify_intent_ai(text: str):
    """Dùng OpenAI để hiểu câu hỏi tự nhiên hơn, trả về 1 intent label."""
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an intent classifier for a Telegram bot helping health product advisors.\n"
                        "Return ONLY ONE of these labels:\n"
                        f"{', '.join(INTENT_LABELS)}\n\n"
                        "Meaning:\n"
                        "- start: greeting or /start\n"
                        "- buy_payment: how to buy/pay/order\n"
                        "- business_escalation: hard business/commission/policy questions\n"
                        "- business_escalation_detail: follow-up message describing the hard question for upline\n"
                        "- channels: official channels, fanpage, website\n"
                        "- combo_health: which combo for a health problem\n"
                        "- product_info: ask about a product by name or description\n"
                        "- product_by_code: ask using a product code (e.g. 070728)\n"
                        "- health_products: ask for products for a health issue (not necessarily a combo)\n"
                        "- menu_* : when pressing menu buttons with those meanings\n"
                        "- fallback: anything else\n"
                        "Answer with ONLY the label, no explanation."
                    )
                },
                {"role": "user", "content": text}
            ]
        )
        label = resp.choices[0].message.content.strip().lower()
        if label in INTENT_LABELS:
            return label
    except Exception as e:
        print("Error classify_intent_ai:", e)
    return None

def classify_intent_rules(text: str):
    t = text.lower().strip()

    # Menu buttons
    if "combo theo vấn đề" in t:
        return "menu_combo"
    if "tra cứu sản phẩm" in t:
        return "menu_product_search"
    if "hướng dẫn mua hàng" in t:
        return "menu_buy_payment"
    if "kết nối tuyến trên" in t:
        return "menu_business_escalation"
    if "kênh & fanpage" in t or "kênh & fan" in t or "kênh và fanpage" in t:
        return "menu_channels"

    # /start
    if t.startswith("/start") or "bắt đầu" in t or "hello" in t:
        return "start"

    # Mã sản phẩm
    code = extract_code(t)
    if code and code in PRODUCT_MAP:
        return "product_by_code"

    # Hỏi mua hàng / thanh toán
    if contains_any(t, ["mua hàng", "đặt hàng", "đặt mua", "thanh toán", "trả tiền", "ship", "giao hàng"]):
        return "buy_payment"

    # Hỏi tuyến trên
    if contains_any(t, ["tuyến trên", "leader", "sponsor", "upline", "khó trả lời", "hỏi giúp"]):
        return "business_escalation"

    # Kênh, fanpage
    if contains_any(t, ["kênh", "kenh", "fanpage", "facebook", "page", "kênh chính thức"]):
        return "channels"

    # Vấn đề sức khỏe (ưu tiên combo trước)
    if contains_any(t, ["tiểu đường", "đái tháo đường", "đường huyết",
                        "dạ dày", "bao tử", "trào ngược", "ợ chua",
                        "cơ xương khớp", "đau khớp", "gout",
                        "huyết áp", "tim mạch",
                        "gan", "men gan", "gan nhiễm mỡ",
                        "tiêu hóa", "rối loạn tiêu hóa", "táo bón"]):
        return "combo_health"

    # Hỏi cụ thể về sản phẩm (theo tên / info)
    if contains_any(t, ["thành phần", "tác dụng", "lợi ích", "cách dùng", "công dụng", "uống như thế nào"]):
        return "product_info"

    # Thử match combo / sản phẩm theo alias
    if find_best_combo(t) is not None:
        return "combo_health"
    if find_best_products(t):
        return "product_info"

    return "fallback"

def classify_intent(text: str):
    label = classify_intent_ai(text)
    if label:
        return label
    return classify_intent_rules(text)

# ============== AI: mượt hóa câu trả lời ==============
def polish_answer_with_ai(answer: str) -> str:
    if not client or not ENABLE_AI_POLISH:
        return answer
    try:
        sys_prompt = (
            "Bạn là trợ lý trả lời cho đội tư vấn viên sản phẩm sức khỏe.\n"
            "Hãy viết lại câu trả lời tiếng Việt cho tự nhiên, rõ ràng, dễ copy gửi cho khách.\n"
            "YÊU CẦU BẮT BUỘC:\n"
            "- KHÔNG thêm bất kỳ claim/lợi ích/thông tin mới nào ngoài nội dung đã có.\n"
            "- GIỮ NGUYÊN tất cả tên sản phẩm, mã sản phẩm, giá, đường link URL, liều dùng.\n"
            "- Nếu có cảnh báo/lưu ý trong nội dung gốc, phải giữ nguyên.\n"
        )
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0.4,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": answer}
            ]
        )
        new_answer = resp.choices[0].message.content.strip()
        return new_answer or answer
    except Exception as e:
        print("Error polish_answer_with_ai:", e)
        return answer

# ============== Format trả lời ==============
def format_combo_answer(combo):
    name     = combo.get("name", "Combo")
    header   = combo.get("header_text", "")
    duration = combo.get("duration_text", "")

    lines = [f"*{name}*"]
    if header:
        lines.append(f"_{header}_")
    if duration:
        lines.append(f"\n⏱ *Thời gian khuyến nghị:* {duration}")

    # Dùng health_tags của combo để nói nhanh combo hỗ trợ gì
    combo_usecase = build_usecase_from_tags(combo.get("health_tags", []))
    if combo_usecase:
        lines.append(f"\n🎯 *Combo này phù hợp:* {combo_usecase}")

    lines.append("\n🧩 *Các sản phẩm trong combo:*")

    products_info = []
    for item in combo.get("products", []):
        code = (item.get("product_code") or "").strip()
        dose = (item.get("dose_text") or "").strip()

        # Lấy thông tin gốc từ PRODUCT_MAP
        p = PRODUCT_MAP.get(code, {}) if code else {}

        pname       = item.get("name")        or p.get("name")        or code
        price       = item.get("price_text")  or p.get("price_text", "")
        url         = item.get("product_url") or p.get("product_url", "")
        benefits    = item.get("benefits_text")    or p.get("benefits_text")    or p.get("benefits", "")
        ingredients = item.get("ingredients_text") or p.get("ingredients_text") or p.get("ingredients", "")
        usage       = item.get("usage_text")       or p.get("usage_text")       or p.get("usage", "")
        tags        = item.get("health_tags")      or p.get("health_tags", [])
        usecase     = build_usecase_from_tags(tags)

        block = f"• *{pname}* ({code})"
        if price:
            block += f"\n  - Giá tham khảo: {price}"
        if benefits:
            block += f"\n  - Lợi ích chính: {benefits}"
        if usecase:
            block += f"\n  - Dùng trong các trường hợp: {usecase}"
        if ingredients:
            block += f"\n  - Thành phần nổi bật: {ingredients}"

        # Phân biệt rõ “cách dùng NSX” và “cách dùng trong combo”
        if usage and dose and usage.strip() != dose.strip():
            block += f"\n  - Cách dùng theo NSX: {usage}"
            block += f"\n  - Cách dùng gợi ý trong combo: {dose}"
        elif dose:
            block += f"\n  - Cách dùng gợi ý: {dose}"
        elif usage:
            block += f"\n  - Cách dùng gợi ý: {usage}"

        if url:
            block += f"\n  - 🔗 Link sản phẩm: {url}"

        products_info.append(block)

    lines.append("\n" + "\n\n".join(products_info))
    lines.append(
        "\n⚠️ Lưu ý: Đây là combo hỗ trợ, không thay thế thuốc điều trị. "
        "TVV nên nhắc khách tuân thủ tư vấn của bác sĩ, kết hợp chế độ ăn uống, vận động, tái khám định kỳ."
    )
    lines.append("\n👉 TVV có thể điều chỉnh câu chữ cho phù hợp với khách hàng cụ thể.")
    return "\n".join(lines)

def format_products_answer(products):
    if not products:
        return (
            "Em chưa tìm được sản phẩm phù hợp trong danh mục hiện có ạ. 🙏\n"
            "Anh/chị có thể gửi rõ hơn tên sản phẩm, mã sản phẩm hoặc vấn đề sức khỏe của khách giúp em."
        )

    lines = ["Dưới đây là *một số sản phẩm phù hợp* trong danh mục:\n"]
    for p in products[:5]:
        name        = p.get("name", "")
        code        = p.get("code", "")
        ingredients = p.get("ingredients_text", "") or p.get("ingredients", "")
        usage       = p.get("usage_text", "")       or p.get("usage", "")
        benefits    = p.get("benefits_text", "")    or p.get("benefits", "")
        url         = p.get("product_url", "")
        price       = p.get("price_text", "")       or p.get("price", "")
        tags        = p.get("health_tags", [])
        usecase     = build_usecase_from_tags(tags)

        block = f"*{name}* ({code})"
        if price:
            block += f"\n- Giá tham khảo: {price}"
        if benefits:
            block += f"\n- Lợi ích chính: {benefits}"
        if usecase:
            block += f"\n- Dùng trong các trường hợp: {usecase}"
        if ingredients:
            block += f"\n- Thành phần nổi bật: {ingredients}"
        if usage:
            block += f"\n- Cách dùng gợi ý: {usage}"
        if url:
            block += f"\n- 🔗 Link sản phẩm: {url}"
        lines.append(block)
        lines.append("")

    lines.append(
        "👉 TVV hãy chọn sản phẩm phù hợp nhất với tình trạng cụ thể của khách, "
        "và luôn nhắc khách đọc kỹ hướng dẫn sử dụng, tham khảo ý kiến bác sĩ khi cần."
    )
    return "\n".join(lines)

def format_product_by_code(code: str):
    p = PRODUCT_MAP.get(code)
    if not p:
        return "Em chưa tìm thấy mã sản phẩm này ạ. Anh/chị kiểm tra lại giúp em mã số nhé. 🙏"

    name        = p.get("name", "")
    ingredients = p.get("ingredients_text", "") or p.get("ingredients", "")
    usage       = p.get("usage_text", "")       or p.get("usage", "")
    benefits    = p.get("benefits_text", "")    or p.get("benefits", "")
    url         = p.get("product_url", "")
    price       = p.get("price_text", "")       or p.get("price", "")
    tags        = p.get("health_tags", [])
    usecase     = build_usecase_from_tags(tags)

    lines = [f"*{name}* ({code})"]
    if price:
        lines.append(f"- Giá tham khảo: {price}")
    if benefits:
        lines.append(f"- Lợi ích chính: {benefits}")
    if usecase:
        lines.append(f"- Dùng trong các trường hợp: {usecase}")
    if ingredients:
        lines.append(f"- Thành phần nổi bật: {ingredients}")
    if usage:
        lines.append(f"- Cách dùng gợi ý: {usage}")
    if url:
        lines.append(f"- 🔗 Link sản phẩm: {url}")
    lines.append(
        "\n👉 TVV có thể chỉnh sửa câu chữ cho phù hợp với khách, "
        "và nhắc khách đọc kỹ hướng dẫn sử dụng, tham khảo ý kiến bác sĩ khi cần."
    )
    return "\n".join(lines)

# ============== Các câu menu / cố định ==============
def answer_start():
    return (
        "*Chào TVV, em là Trợ lý AI hỗ trợ kinh doanh & sản phẩm.* 🤖\n\n"
        "Anh/chị có thể:\n"
        "• Hỏi theo vấn đề sức khỏe: _\"Khách bị tiểu đường thì dùng combo nào?\"_\n"
        "• Hỏi theo sản phẩm: _\"Cho em thành phần, cách dùng của mã 070728\"_\n"
        "• Hỏi quy trình: _\"Hướng dẫn mua hàng / thanh toán thế nào?\"_\n"
        "• Nhờ tuyến trên: _\"Câu này khó, cho em xin kết nối leader?\"_\n\n"
        "Hoặc bấm các nút menu bên dưới để thao tác nhanh. ❤️"
    )

def answer_menu_combo():
    return (
        "🧩 *Combo theo vấn đề sức khỏe*\n\n"
        "Anh/chị hãy gõ câu dạng:\n"
        "- \"Khách *tiểu đường* thì dùng combo nào?\"\n"
        "- \"Khách bị *cơ xương khớp* đau nhiều thì tư vấn combo gì?\"\n"
        "- \"Khách bị *huyết áp, tim mạch* thì nên dùng gì?\""
    )

def answer_menu_product_search():
    return (
        "🔎 *Tra cứu sản phẩm*\n\n"
        "Anh/chị có thể hỏi:\n"
        "- \"Cho em info sản phẩm *ANTISWEET*?\"\n"
        "- \"Thành phần, cách dùng của mã *070728* là gì?\"\n"
        "- \"Sản phẩm nào hỗ trợ *tiểu đường / men gan / xương khớp*?\""
    )

def answer_buy_payment():
    lines = []
    lines.append("*Hướng dẫn mua hàng & thanh toán* 🛒")
    lines.append("\n1️⃣ *Cách mua hàng:*")
    lines.append(f"- Đặt trực tiếp trên website: {LINK_WEBSITE}")
    lines.append("- Nhờ TVV tạo đơn hàng trên hệ thống.")
    lines.append("- Gọi Hotline để được hỗ trợ tạo đơn.")
    lines.append("\n2️⃣ *Các bước đặt trên website (gợi ý):*")
    lines.append("   1. Truy cập website.")
    lines.append("   2. Chọn sản phẩm → bấm *“Thêm vào giỏ”*.")
    lines.append("   3. Vào *Giỏ hàng* → kiểm tra sản phẩm.")
    lines.append("   4. Bấm *“Thanh toán”* → nhập thông tin nhận hàng.")
    lines.append("   5. Chọn hình thức thanh toán phù hợp.")
    lines.append("\n3️⃣ *Hình thức thanh toán thường dùng:*")
    lines.append("- 💵 Thanh toán khi nhận hàng (COD).")
    lines.append("- 💳 Chuyển khoản ngân hàng (theo số TK chính thức của công ty).")
    lines.append("- 📱 Thanh toán online (QR, ví điện tử…) nếu có.")
    return "\n".join(lines)

def answer_business_escalation():
    return (
        "*Kết nối tuyến trên khi gặp câu hỏi khó* ☎️\n\n"
        "Anh/chị hãy gửi tiếp *1 tin nhắn nữa* mô tả rõ:\n"
        "- Câu hỏi / tình huống cụ thể của khách\n"
        "- Phương án anh/chị đang phân vân hoặc đã trả lời thử\n"
        "- Mức độ gấp (vd: cần hỗ trợ trong hôm nay)\n\n"
        "Ngay sau tin nhắn đó, em sẽ *chuyển nguyên văn* cho tuyến trên để hỗ trợ.\n"
        f"Nếu thật sự gấp, anh/chị có thể gọi thêm Hotline: *{HOTLINE_TUYEN_TREN}*."
    )

def answer_channels():
    return (
        "*Kênh & Fanpage chính thức của công ty* 📢\n\n"
        f"- 📺 Kênh Telegram: {LINK_KENH_TELEGRAM}\n"
        f"- 👍 Fanpage Facebook: {LINK_FANPAGE}\n"
        f"- 🌐 Website: {LINK_WEBSITE}\n\n"
        "👉 TVV nên ưu tiên gửi khách các đường link chính thức này."
    )

def answer_fallback():
    return (
        "Hiện tại em chưa hiểu rõ câu hỏi hoặc chưa có dữ liệu cho nội dung này ạ. 🙏\n\n"
        "Anh/chị có thể:\n"
        "- Mô tả *cụ thể hơn* tình trạng của khách, hoặc\n"
        "- Hỏi dạng: \"Khách bị *tiểu đường*...\", \"Khách bị *đau dạ dày*...\", "
        "\"*Cách mua hàng*?\", \"*Thanh toán thế nào*?\", hoặc\n"
        "- Bấm nút *Kết nối tuyến trên* để em hướng dẫn liên hệ leader."
    )

# ============== Logging lên Google Sheets ==============
def log_to_sheet(payload: dict):
    if not LOG_SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(LOG_SHEET_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print("Error log_to_sheet:", e)

# ============== Webhook chính ==============
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify(ok=True)

    chat_id   = message["chat"]["id"]
    text      = message.get("text", "") or ""
    from_user = message.get("from", {}) or {}
    user_name = (from_user.get("first_name", "") + " " +
                 from_user.get("last_name", "")).strip() or from_user.get("username", "")

    # Nếu là tin nhắn từ nhóm / tài khoản tuyến trên
    # → xử lý /reply <chat_id> ... hoặc reply vào tin forward
    if UPLINE_CHAT_ID and str(chat_id) == str(UPLINE_CHAT_ID) and text.strip():
        target_chat_id = None
        reply_body = None

        # Cách 1: /reply <chat_id> Nội dung...
        m = re.match(r"^/reply\s+(-?\d+)\s+(.+)", text.strip(), re.DOTALL | re.IGNORECASE)
        if m:
            target_chat_id = int(m.group(1))
            reply_body = m.group(2).strip()

        else:
            # Cách 2: reply vào tin "YÊU CẦU HỖ TRỢ TUYẾN TRÊN" mà bot đã gửi trong nhóm
            reply_msg = message.get("reply_to_message") or {}
            base_text = reply_msg.get("text") or ""
            m2 = re.search(r"chat_id:\s*`(-?\d+)`", base_text)
            if m2:
                target_chat_id = int(m2.group(1))
                reply_body = text.strip()

        if target_chat_id and reply_body:
            tvv_reply = f"*Trả lời từ tuyến trên:* 👇\n\n{reply_body}"
            tvv_reply = polish_answer_with_ai(tvv_reply)
            # Gửi cho TVV kèm keyboard
            send_message(target_chat_id, tvv_reply, reply_markup=MAIN_KEYBOARD)

            # Log lên Google Sheets (intent: upline_reply)
            log_payload = {
                "chat_id": target_chat_id,           # chat của TVV
                "user_name": user_name,              # tên người trả lời (tuyến trên)
                "text": reply_body,
                "intent": "upline_reply",
                "matched_combo_id": "",
                "matched_combo_name": "",
                "matched_product_code": "",
                "matched_product_name": "",
                "upline_name": user_name,
                "from_upline_chat_id": chat_id,
            }
            log_to_sheet(log_payload)

        # Dù parse được hay không thì cũng kết thúc xử lý ở đây
        return jsonify(ok=True)

    # Nếu không phải từ UPLINE_CHAT_ID → xử lý như TVV bình thường

    if not text:
        send_message(chat_id, "Hiện tại em chỉ hiểu tin nhắn dạng text thôi ạ. 🙏", reply_markup=MAIN_KEYBOARD)
        return jsonify(ok=True)

    # ====== Nếu TVV đang ở chế độ chờ mô tả câu hỏi cho tuyến trên ======
    if ESCALATION_PENDING.get(chat_id):
        ESCALATION_PENDING.pop(chat_id, None)

        # Gửi sang nhóm / leader tuyến trên
        if UPLINE_CHAT_ID:
            notify = (
                "🔔 *YÊU CẦU HỖ TRỢ TUYẾN TRÊN*\n\n"
                f"- Từ TVV: *{user_name}* (chat_id: `{chat_id}`)\n"
                f"- Nội dung:\n{text}"
            )
            try:
                send_message(UPLINE_CHAT_ID, notify)
            except Exception as e:
                print("Error forward to upline:", e)

        confirm = (
            "Em đã ghi nhận và *chuyển nội dung này cho tuyến trên* rồi ạ. ✅\n"
            f"Nếu cần gấp, anh/chị có thể gọi thêm Hotline: *{HOTLINE_TUYEN_TREN}*.\n"
            "Khi tuyến trên phản hồi, anh/chị nhớ cập nhật lại cho khách nhé."
        )
        confirm = polish_answer_with_ai(confirm)
        send_message(chat_id, confirm, reply_markup=MAIN_KEYBOARD)

        # Log intent chi tiết tuyến trên
        log_payload = {
            "chat_id": chat_id,
            "user_name": user_name,
            "text": text,
            "intent": "business_escalation_detail",
            "matched_combo_id": "",
            "matched_combo_name": "",
            "matched_product_code": "",
            "matched_product_name": "",
        }
        log_to_sheet(log_payload)

        return jsonify(ok=True)

    # ====== Bình thường: phân loại intent ======
    intent = classify_intent(text)

    matched_combo_id      = ""
    matched_combo_name    = ""
    matched_product_code  = ""
    matched_product_name  = ""

    # Xử lý intent
    if intent == "start":
        reply = answer_start()

    elif intent in ("menu_combo",):
        reply = answer_menu_combo()

    elif intent in ("menu_product_search",):
        reply = answer_menu_product_search()

    elif intent in ("menu_buy_payment", "buy_payment"):
        reply = answer_buy_payment()

    elif intent in ("menu_business_escalation", "business_escalation"):
        # Đánh dấu: tin nhắn kế tiếp sẽ là nội dung nhờ tuyến trên
        ESCALATION_PENDING[chat_id] = True
        reply = answer_business_escalation()

    elif intent in ("menu_channels", "channels"):
        reply = answer_channels()

    elif intent == "product_by_code":
        code = extract_code(text)
        if code and code in PRODUCT_MAP:
            reply = format_product_by_code(code)
            matched_product_code = code
            matched_product_name = PRODUCT_MAP[code].get("name", "")
        else:
            reply = "Em chưa tìm được mã sản phẩm này, anh/chị kiểm tra lại giúp em nhé. 🙏"

    elif intent == "combo_health":
        combo = find_combo_by_health_keyword(text)
        if combo:
            reply = format_combo_answer(combo)
            matched_combo_id   = combo.get("id", "")
            matched_combo_name = combo.get("name", "")
        else:
            # Nếu không tìm được combo, thử trả sản phẩm theo vấn đề sức khỏe
            products = find_products_by_health(text)
            reply    = format_products_answer(products)
            if products:
                matched_product_code = products[0].get("code", "")
                matched_product_name = products[0].get("name", "")

    elif intent == "health_products":
        products = find_products_by_health(text)
        reply    = format_products_answer(products)
        if products:
            matched_product_code = products[0].get("code", "")
            matched_product_name = products[0].get("name", "")

    elif intent == "product_info":
        products = find_best_products(text)
        reply    = format_products_answer(products)
        if products:
            matched_product_code = products[0].get("code", "")
            matched_product_name = products[0].get("name", "")

    else:
        reply = answer_fallback()

    # Mượt hóa bằng OpenAI (nếu bật)
    reply = polish_answer_with_ai(reply)

    # Gửi lại cho TVV kèm keyboard
    send_message(chat_id, reply, reply_markup=MAIN_KEYBOARD)

    # Log lên Google Sheets
    log_payload = {
        "chat_id": chat_id,
        "user_name": user_name,
        "text": text,
        "intent": intent,
        "matched_combo_id": matched_combo_id,
        "matched_combo_name": matched_combo_name,
        "matched_product_code": matched_product_code,
        "matched_product_name": matched_product_name,
    }
    log_to_sheet(log_payload)

    return jsonify(ok=True)

    # ====== Bình thường: phân loại intent ======
    intent = classify_intent(text)

    matched_combo_id      = ""
    matched_combo_name    = ""
    matched_product_code  = ""
    matched_product_name  = ""

    # Xử lý intent
    if intent == "start":
        reply = answer_start()

    elif intent in ("menu_combo",):
        reply = answer_menu_combo()

    elif intent in ("menu_product_search",):
        reply = answer_menu_product_search()

    elif intent in ("menu_buy_payment", "buy_payment"):
        reply = answer_buy_payment()

    elif intent in ("menu_business_escalation", "business_escalation"):
        # Đánh dấu: tin nhắn kế tiếp sẽ là nội dung nhờ tuyến trên
        ESCALATION_PENDING[chat_id] = True
        reply = answer_business_escalation()

    elif intent in ("menu_channels", "channels"):
        reply = answer_channels()

    elif intent == "product_by_code":
        code = extract_code(text)
        if code and code in PRODUCT_MAP:
            reply = format_product_by_code(code)
            matched_product_code = code
            matched_product_name = PRODUCT_MAP[code].get("name", "")
        else:
            reply = "Em chưa tìm được mã sản phẩm này, anh/chị kiểm tra lại giúp em nhé. 🙏"

    elif intent == "combo_health":
        combo = find_combo_by_health_keyword(text)
        if combo:
            reply = format_combo_answer(combo)
            matched_combo_id   = combo.get("id", "")
            matched_combo_name = combo.get("name", "")
        else:
            # Nếu không tìm được combo, thử trả sản phẩm theo vấn đề sức khỏe
            products = find_products_by_health(text)
            reply    = format_products_answer(products)
            if products:
                matched_product_code = products[0].get("code", "")
                matched_product_name = products[0].get("name", "")

    elif intent == "health_products":
        products = find_products_by_health(text)
        reply    = format_products_answer(products)
        if products:
            matched_product_code = products[0].get("code", "")
            matched_product_name = products[0].get("name", "")

    elif intent == "product_info":
        products = find_best_products(text)
        reply    = format_products_answer(products)
        if products:
            matched_product_code = products[0].get("code", "")
            matched_product_name = products[0].get("name", "")

    else:
        reply = answer_fallback()

    # Mượt hóa bằng OpenAI (nếu bật)
    reply = polish_answer_with_ai(reply)

    # Gửi lại cho TVV kèm keyboard
    send_message(chat_id, reply, reply_markup=MAIN_KEYBOARD)

    # Log lên Google Sheets
    log_payload = {
        "chat_id": chat_id,
        "user_name": user_name,
        "text": text,
        "intent": intent,
        "matched_combo_id": matched_combo_id,
        "matched_combo_name": matched_combo_name,
        "matched_product_code": matched_product_code,
        "matched_product_name": matched_product_name,
    }
    log_to_sheet(log_payload)

    return jsonify(ok=True)

@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)


