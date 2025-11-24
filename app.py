import os
import json
import re
import unicodedata
from typing import List, Dict, Any, Optional

import requests
from flask import Flask, request, jsonify

# ============ OpenAI client (intent + làm mượt) ============
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# ============ ENV ============
TELEGRAM_TOKEN        = os.getenv("TELEGRAM_TOKEN", "")
LOG_SHEET_WEBHOOK_URL = os.getenv("LOG_SHEET_WEBHOOK_URL", "")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_TOKEN trong .env")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

client = None
if OPENAI_API_KEY and OPENAI_AVAILABLE:
    client = OpenAI(api_key=OPENAI_API_KEY)

# ============ Flask ============
app = Flask(__name__)

# ============ Đường dẫn data ============
BASE_DIR = os.path.dirname(__file__)
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")
COMBOS_FILE   = os.path.join(BASE_DIR, "combos.json")


# ============ Helper chung ============
def norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def normalize_for_match(s: str) -> str:
    """Lower + bỏ dấu + loại ký tự lạ → dùng cho so khớp alias."""
    if not s:
        return ""
    s = str(s).lower().strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def extract_code_from_text(text: str) -> Optional[str]:
    """Bắt mã sản phẩm dạng 5–6 chữ số (VD: 070728, 01590)"""
    if not text:
        return None
    m = re.findall(r"\b\d{5,6}\b", text)
    return m[0] if m else None


# ============ Load & build index từ JSON ============
def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


data_products = load_json(PRODUCTS_FILE)
data_combos   = load_json(COMBOS_FILE)

PRODUCTS: List[Dict[str, Any]] = data_products.get("products", data_products)
COMBOS:   List[Dict[str, Any]] = data_combos.get("combos", data_combos)

# Map code → product
PRODUCT_MAP: Dict[str, Dict[str, Any]] = {}
# alias index: alias_norm → set(code)
PRODUCT_ALIAS_INDEX: Dict[str, set] = {}

for p in PRODUCTS:
    code = str(p.get("code", "")).lstrip("#").strip()
    if not code:
        continue
    p["code"] = code
    PRODUCT_MAP[code] = p

    aliases = set()
    name = p.get("name", "")
    aliases.add(name)
    aliases.add(code)
    # nếu file đã có aliases thì dùng luôn
    for a in p.get("aliases", []):
        aliases.add(a)

    # auto thêm các biến thể tách bởi (), -, /
    extra = re.findall(r"[\w\u00C0-\u017F\-\/]+", name)
    for e in extra:
        aliases.add(e)

    # index
    for a in aliases:
        na = normalize_for_match(a)
        if not na:
            continue
        PRODUCT_ALIAS_INDEX.setdefault(na, set()).add(code)

# combos index
COMBO_LIST: List[Dict[str, Any]] = []
COMBO_ALIAS_INDEX: Dict[str, List[Dict[str, Any]]] = {}
for c in COMBOS:
    cid = c.get("id") or normalize_for_match(c.get("name", "") or "")
    c["id"] = cid
    COMBO_LIST.append(c)

    aliases = set()
    aliases.add(c.get("name", ""))
    for a in c.get("aliases", []):
        aliases.add(a)
    for a in aliases:
        na = normalize_for_match(a)
        if not na:
            continue
        COMBO_ALIAS_INDEX.setdefault(na, []).append(c)


# ============ Tìm combo / sản phẩm ============

def find_product_by_code(code: str) -> Optional[Dict[str, Any]]:
    code = (code or "").lstrip("#").strip()
    return PRODUCT_MAP.get(code)


def find_products_by_alias(text: str, limit: int = 5) -> List[Dict[str, Any]]:
    t = normalize_for_match(text)
    results = []
    seen = set()

    # match alias full (alias_norm in text_norm)
    for alias_norm, codes in PRODUCT_ALIAS_INDEX.items():
        if alias_norm and alias_norm in t:
            for c in codes:
                if c not in seen and c in PRODUCT_MAP:
                    seen.add(c)
                    results.append(PRODUCT_MAP[c])
                    if len(results) >= limit:
                        return results

    # nếu chưa thấy gì → thử match từng token
    if not results:
        tokens = t.split()
        for alias_norm, codes in PRODUCT_ALIAS_INDEX.items():
            if any(tok in alias_norm for tok in tokens):
                for c in codes:
                    if c not in seen and c in PRODUCT_MAP:
                        seen.add(c)
                        results.append(PRODUCT_MAP[c])
                        if len(results) >= limit:
                            return results
    return results


def find_combos_by_issue(text: str, limit: int = 3) -> List[Dict[str, Any]]:
    t = normalize_for_match(text)
    results = []
    seen = set()
    # match alias combo
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


# ============ OpenAI: phân loại intent + làm mượt ============

INTENT_LABELS = [
    "start",
    "product_by_code",
    "product_info",
    "combo_health",
    "buy_payment",
    "business_escalation",
    "channels",
    "fallback",
    "menu_combo",
    "menu_product_search",
    "menu_buy_payment",
    "menu_business_escalation",
    "menu_channels"
]


def classify_intent_ai(text: str) -> Optional[str]:
    if not client:
        return None
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an intent classifier for a Telegram bot that helps health product advisors.\n"
                    f"Return EXACTLY ONE of these labels: {', '.join(INTENT_LABELS)}.\n"
                    "- start: greetings or /start\n"
                    "- product_by_code: asking by product code (e.g. 070728)\n"
                    "- product_info: asking about product name/usage/ingredients/benefits\n"
                    "- combo_health: asking which combo for a health problem\n"
                    "- buy_payment: how to buy, pay, order\n"
                    "- business_escalation: hard business/policy questions → need upline\n"
                    "- channels: ask about official channels, fanpage\n"
                    "- menu_*: when pressing keyboard buttons\n"
                    "- fallback: everything else\n"
                    "Answer with label only."
                )
            },
            {"role": "user", "content": text}
        ]
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            temperature=0
        )
        label = resp.choices[0].message.content.strip().lower()
        if label in INTENT_LABELS:
            return label
        return None
    except Exception as e:
        print("classify_intent_ai error:", e)
        return None


def classify_intent_rules(text: str) -> str:
    t = text.lower().strip()

    if t.startswith("/start") or "bắt đầu" in t or "hello" in t:
        return "start"

    if "combo theo vấn đề" in t:
        return "menu_combo"
    if "tra cứu sản phẩm" in t:
        return "menu_product_search"
    if "hướng dẫn mua hàng" in t:
        return "menu_buy_payment"
    if "kết nối tuyến trên" in t:
        return "menu_business_escalation"
    if "kênh & fanpage" in t or "kênh và fanpage" in t:
        return "menu_channels"

    # code?
    if extract_code_from_text(text):
        return "product_by_code"

    # từ khóa sức khỏe phổ biến
    health_keywords = [
        "tiểu đường", "đái tháo đường", "đường huyết",
        "dạ dày", "bao tử", "trào ngược", "ợ chua",
        "tiêu hóa", "tiêu hoá", "táo bón",
        "gan", "men gan", "gan nhiễm mỡ",
        "xương khớp", "đau khớp", "gout", "thoái hóa",
        "tim mạch", "huyết áp",
        "thải độc", "detox", "ung thư",
    ]
    if any(k in t for k in health_keywords):
        return "combo_health"

    # rule cho mua hàng
    if any(k in t for k in ["mua hàng", "đặt hàng", "thanh toán", "ship", "giao hàng"]):
        return "buy_payment"

    # tuyến trên
    if any(k in t for k in ["tuyến trên", "leader", "upline", "khó trả lời"]):
        return "business_escalation"

    # kênh chính thức
    if any(k in t for k in ["kênh", "kenh", "fanpage", "facebook", "page"]):
        return "channels"

    # nếu tìm được combo theo alias
    if find_combos_by_issue(t):
        return "combo_health"
    # nếu tìm được sản phẩm theo alias
    if find_products_by_alias(t):
        return "product_info"

    return "fallback"


def classify_intent(text: str) -> str:
    label = classify_intent_ai(text)
    if label:
        return label
    return classify_intent_rules(text)


def polish_answer_with_ai(text: str) -> str:
    if not client:
        return text
    try:
        sys = (
            "Bạn là trợ lý viết lại câu trả lời cho TVV.\n"
            "Hãy viết lại câu trả lời tiếng Việt rõ ràng, dễ hiểu, lịch sự.\n"
            "KHÔNG được thêm claim, công dụng, thông tin mới ngoài nội dung đã có.\n"
            "Giữ nguyên tên sản phẩm, mã, liều dùng, giá, link."
        )
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0.3,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": text}
            ]
        )
        out = resp.choices[0].message.content.strip()
        return out or text
    except Exception as e:
        print("polish_answer_with_ai error:", e)
        return text


# ============ Format trả lời ============

def format_product(p: Dict[str, Any]) -> str:
    code = p.get("code", "")
    name = p.get("name", "")
    price = p.get("price_text") or p.get("price") or ""
    url   = p.get("product_url") or p.get("link") or ""
    ing   = p.get("ingredients_text") or p.get("ingredients") or ""
    use   = p.get("usage_text") or p.get("usage") or ""
    ben   = p.get("benefits_text") or p.get("benefits") or ""

    parts = [f"*{name}* (`{code}`)"]
    if price:
        parts.append(f"- Giá tham khảo: {price}")
    if ben:
        parts.append(f"- Lợi ích chính: {ben}")
    if ing:
        parts.append(f"- Thành phần nổi bật: {ing}")
    if use:
        parts.append(f"- Cách dùng gợi ý: {use}")
    if url:
        parts.append(f"- 🔗 Link sản phẩm: {url}")
    parts.append("\n👉 TVV chỉnh lại câu chữ cho phù hợp với khách.")
    return "\n".join(parts)


def format_products_list(prods: List[Dict[str, Any]]) -> str:
    if not prods:
        return "Em chưa tìm được sản phẩm phù hợp trong danh mục hiện có ạ. 🙏"

    lines = ["Dưới đây là *một số sản phẩm phù hợp*:\n"]
    for p in prods[:5]:
        lines.append(format_product(p))
        lines.append("")  # dòng trống
    return "\n".join(lines)


def format_combo(c: Dict[str, Any]) -> str:
    name = c.get("name", "")
    duration = c.get("duration_text", "")
    header = c.get("header_text", "")

    lines = [f"*{name}*"]
    if duration:
        lines.append(f"⏱ *Thời gian dùng khuyến nghị:* {duration}")
    if header:
        lines.append(header)

    lines.append("\n*Các sản phẩm trong combo:*")
    for item in c.get("products", []):
        code = item.get("product_code", "")
        pname = item.get("name", "")
        price = item.get("price_text", "")
        url   = item.get("product_url", "")
        dose  = item.get("dose_text", "")

        block = f"- *{pname}* (`{code}`)"
        if price:
            block += f"\n  • Giá tham khảo: {price}"
        if dose:
            block += f"\n  • Cách dùng gợi ý: {dose}"
        if url:
            block += f"\n  • Link: {url}"
        lines.append(block)

    lines.append(
        "\n⚠️ Đây là combo hỗ trợ, không thay thế thuốc điều trị. "
        "TVV nhắc khách tuân thủ phác đồ của bác sĩ, kết hợp ăn uống & vận động."
    )
    return "\n".join(lines)


# ============ Telegram helpers ============

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


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=10)
    except Exception as e:
        print("send_message error:", e)


def log_to_sheet(payload: Dict[str, Any]):
    if not LOG_SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(LOG_SHEET_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print("log_to_sheet error:", e)


# ============ Trả lời các intent cố định ============

def answer_start():
    return (
        "*Chào TVV, em là Trợ lý AI hỗ trợ sản phẩm & kinh doanh.* 🤖\n\n"
        "Anh/chị có thể:\n"
        "• Hỏi theo vấn đề sức khỏe: _\"Khách bị tiểu đường thì dùng combo nào?\"_\n"
        "• Hỏi theo mã: _\"Cho em info mã 070728\"_\n"
        "• Hỏi theo sản phẩm: _\"Thành phần/cách dùng của ANTISWEET?\"_\n"
        "• Hỏi quy trình: _\"Hướng dẫn mua hàng / thanh toán?\"_\n\n"
        "Hoặc dùng nhanh các nút bên dưới. ❤️"
    )


def answer_buy_payment():
    return (
        "*Hướng dẫn mua hàng & thanh toán* 🛒\n\n"
        "1️⃣ *Cách mua hàng:*\n"
        "- Khách đặt qua TVV (anh/chị tạo đơn trên hệ thống).\n"
        "- Hoặc khách tự đặt trên website chính thức (nếu có).\n\n"
        "2️⃣ *Thanh toán thường dùng:*\n"
        "- Thanh toán khi nhận hàng (COD) nếu hỗ trợ.\n"
        "- Chuyển khoản tài khoản công ty.\n"
        "- Thanh toán online (QR / ví điện tử) nếu có.\n"
    )


def answer_business_escalation():
    return (
        "*Kết nối tuyến trên khi gặp câu hỏi khó* ☎️\n\n"
        "- TVV chụp màn hình câu hỏi + phương án trả lời dự kiến.\n"
        "- Gửi cho tuyến trên / leader trong nhóm nội bộ.\n"
        "- Với câu hỏi về *chính sách, hoa hồng, pháp lý*: nên chuyển khách sang hotline/tuyến trên."
    )


def answer_channels():
    return (
        "*Kênh & Fanpage chính thức* 📢\n\n"
        "- Fanpage: (điền link chính thức)\n"
        "- Kênh Telegram/Zalo: (điền link nếu có)\n"
        "- Website: (điền link website)\n\n"
        "👉 TVV nên ưu tiên gửi khách các kênh chính thức này."
    )


def answer_fallback():
    return (
        "Hiện tại em chưa hiểu rõ câu hỏi hoặc chưa có dữ liệu cho nội dung này ạ. 🙏\n\n"
        "Anh/chị có thể:\n"
        "- Gõ rõ hơn mã sản phẩm (VD: 070728) hoặc tên sản phẩm.\n"
        "- Mô tả vấn đề sức khỏe: *tiểu đường, dạ dày, tim mạch, xương khớp, gan…*\n"
        "- Hoặc bấm các nút menu bên dưới."
    )


# ============ WEBHOOK ============

@app.route("/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True)

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = message.get("chat", {}).get("id")
    from_user = message.get("from", {})
    user_name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip() or from_user.get("username", "")
    text = message.get("text", "") or ""

    if not chat_id or not text:
        return jsonify(ok=True)

    intent = classify_intent(text)

    matched_combo_id = ""
    matched_combo_name = ""
    matched_product_code = ""
    matched_product_name = ""

    # Xử lý intent
    if intent == "start":
        reply = answer_start()

    elif intent in ("menu_combo",):
        reply = "Anh/chị hãy gõ vấn đề sức khỏe khách đang gặp (VD: *tiểu đường, dạ dày, xương khớp, huyết áp...*)."

    elif intent in ("menu_product_search",):
        reply = (
            "Anh/chị có thể hỏi:\n"
            "- \"Cho em info mã *070728*\".\n"
            "- \"Thành phần/cách dùng của *tên sản phẩm*\".\n"
            "- Hoặc mô tả vấn đề sức khỏe để em gợi ý sản phẩm phù hợp."
        )

    elif intent in ("menu_buy_payment", "buy_payment"):
        reply = answer_buy_payment()

    elif intent in ("menu_business_escalation", "business_escalation"):
        reply = answer_business_escalation()

    elif intent in ("menu_channels", "channels"):
        reply = answer_channels()

    elif intent == "product_by_code":
        code = extract_code_from_text(text)
        p = find_product_by_code(code) if code else None
        if p:
            reply = format_product(p)
            matched_product_code = p.get("code", "")
            matched_product_name = p.get("name", "")
        else:
            reply = "Em chưa tìm được sản phẩm với mã này ạ. Anh/chị kiểm tra lại giúp em mã số nhé. 🙏"

    elif intent == "combo_health":
        combos = find_combos_by_issue(text)
        if combos:
            c = combos[0]
            reply = format_combo(c)
            matched_combo_id = c.get("id", "")
            matched_combo_name = c.get("name", "")
        else:
            # Nếu không có combo, thử trả sản phẩm theo issue
            prods = find_products_by_alias(text)
            if prods:
                reply = format_products_list(prods)
                matched_product_code = prods[0].get("code", "")
                matched_product_name = prods[0].get("name", "")
            else:
                reply = (
                    "Em chưa tìm được combo/sản phẩm phù hợp với mô tả này ạ. 🙏\n"
                    "Anh/chị thử ghi rõ hơn vấn đề sức khỏe hoặc mã sản phẩm nhé."
                )

    elif intent == "product_info":
        prods = find_products_by_alias(text)
        if prods:
            reply = format_products_list(prods)
            matched_product_code = prods[0].get("code", "")
            matched_product_name = prods[0].get("name", "")
        else:
            reply = (
                "Em chưa tìm được sản phẩm phù hợp trong danh mục hiện có ạ. 🙏\n"
                "Anh/chị thử gửi mã sản phẩm (VD: 070728) hoặc tên đầy đủ giúp em."
            )

    else:
        reply = answer_fallback()

    # Làm mượt bằng OpenAI (nếu có)
    reply = polish_answer_with_ai(reply)

    # Gửi message kèm keyboard
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
    return jsonify({
        "ok": True,
        "products_count": len(PRODUCTS),
        "combos_count": len(COMBOS)
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)), debug=True)
