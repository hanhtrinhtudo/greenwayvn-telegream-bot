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

ENABLE_AI_POLISH      = os.getenv("ENABLE_AI_POLISH", "true").lower() == "true"

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

PRODUCTS = PRODUCTS_DATA.get("products", [])
COMBOS   = COMBOS_DATA.get("combos", [])

PRODUCT_MAP = {p.get("code"): p for p in PRODUCTS if p.get("code")}

# ============== Mapping vấn đề sức khỏe → combo / sản phẩm (anh có thể bổ sung dần) ==============
# Gợi ý: anh sửa / thêm cho đúng với chiến lược công ty.

# Keyword → id combo (trong combos.json)
HEALTH_KEYWORDS_COMBO = {
    "tiểu đường": "combo_tieu_duong",
    "đái tháo đường": "combo_tieu_duong",
    "đường huyết": "combo_tieu_duong",

    "cơ xương khớp": "combo_co_xuong_khop",
    "đau khớp": "combo_co_xuong_khop",
    "gout": "combo_co_xuong_khop",

    "huyết áp": "combo_huyet_ap_tim_mach",
    "tim mạch": "combo_huyet_ap_tim_mach",

    "gan": "combo_cai_thien_chuc_nang_gan",
    "men gan": "combo_cai_thien_chuc_nang_gan",
    "gan nhiễm mỡ": "combo_cai_thien_chuc_nang_gan",

    "tiêu hóa": "combo_cai_thien_he_tieu_hoa",
    "rối loạn tiêu hóa": "combo_cai_thien_he_tieu_hoa",
    "táo bón": "combo_cai_thien_he_tieu_hoa",

    "thừa cân": "combo_thua_can_beo_phi",
    "béo phì": "combo_thua_can_beo_phi",
}

# Keyword → danh sách mã sản phẩm (nếu anh muốn trả theo sản phẩm, không dùng combo)
HEALTH_KEYWORDS_PRODUCTS = {
    # Ví dụ: tiểu đường – một vài sản phẩm chính
    "tiểu đường": ["070728", "070729", "07124"],
    "đái tháo đường": ["070728", "070729", "07124"],
    "đường huyết": ["070728", "070729", "07124"],

    # Dạ dày / tiêu hóa
    "dạ dày": [],
    "trào ngược": [],
    "ợ chua": [],

    # Gan
    "gan": [],
    "men gan": [],

    # Xương khớp
    "đau khớp": [],
    "gout": [],
    "thoái hóa": [],
    # ...
    # Anh có thể tự bổ sung thêm / chỉnh danh sách mã sản phẩm cho chuẩn.
}

# Build map combo_id → combo
COMBO_ID_MAP = {c.get("id"): c for c in COMBOS if c.get("id")}

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

def find_best_combo(text: str):
    text = text.lower()
    best_combo = None
    score_best = 0
    for combo in COMBOS:
        aliases = combo.get("aliases", [])
        score = sum(1 for kw in aliases if kw.lower() in text)
        if score > score_best:
            score_best = score
            best_combo = combo
    return best_combo

def find_combo_by_health_keyword(text: str):
    t = text.lower()
    # Ưu tiên map keyword → combo_id
    for kw, combo_id in HEALTH_KEYWORDS_COMBO.items():
        if kw in t:
            combo = COMBO_ID_MAP.get(combo_id)
            if combo:
                return combo
    # Nếu không match map, fallback theo aliases trong combos.json
    return find_best_combo(text)

def find_products_by_health(text: str):
    t = text.lower()
    codes = set()
    for kw, code_list in HEALTH_KEYWORDS_PRODUCTS.items():
        if kw in t:
            for c in code_list:
                codes.add(c)
    # Convert sang list sản phẩm
    results = []
    for c in codes:
        p = PRODUCT_MAP.get(c)
        if p:
            results.append(p)
    # Nếu HEALTH_KEYWORDS_PRODUCTS chưa khai đủ → fallback bằng alias
    if not results:
        results = find_best_products(t)
    return results

def find_best_products(text: str):
    text = text.lower()
    matches = []
    for p in PRODUCTS:
        aliases = p.get("aliases", [])
        if any(a.lower() in text for a in aliases):
            matches.append(p)
    return matches

# ============== AI: phân loại intent ==============
INTENT_LABELS = [
    "start",
    "buy_payment",
    "business_escalation",
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
        # Mình sẽ dùng combo_health, còn trong handler có thể thêm sản phẩm nếu cần
        return "combo_health"

    # Hỏi cụ thể về sản phẩm (theo tên)
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
    name    = combo.get("name", "Combo")
    header  = combo.get("header_text", "")
    duration = combo.get("duration_text", "")

    lines = [f"*{name}*"]
    if header:
        lines.append(f"_{header}_")
    if duration:
        lines.append(f"\n⏱ *Thời gian khuyến nghị:* {duration}")

    lines.append("\n🧩 *Các sản phẩm trong combo:*")

    products_info = []
    for item in combo.get("products", []):
        code = item.get("product_code")
        dose = (item.get("dose_text") or "").strip()
        p    = PRODUCT_MAP.get(code, {})
        pname  = item.get("name") or p.get("name") or code
        price  = item.get("price_text") or p.get("price_text", "")
        url    = item.get("product_url") or p.get("product_url", "")

        block = f"• *{pname}* ({code})"
        if price:
            block += f"\n  - Giá tham khảo: {price}"
        if dose:
            block += f"\n  - Cách dùng gợi ý: {dose}"
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
        name       = p.get("name", "")
        code       = p.get("code", "")
        ingredients= p.get("ingredients_text", "")
        usage      = p.get("usage_text", "")
        benefits   = p.get("benefits_text", "")
        url        = p.get("product_url", "")
        price      = p.get("price_text", "")

        block = f"*{name}* ({code})"
        if price:
            block += f"\n- Giá tham khảo: {price}"
        if benefits:
            block += f"\n- Lợi ích chính: {benefits}"
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

    name       = p.get("name", "")
    ingredients= p.get("ingredients_text", "")
    usage      = p.get("usage_text", "")
    benefits   = p.get("benefits_text", "")
    url        = p.get("product_url", "")
    price      = p.get("price_text", "")

    lines = [f"*{name}* ({code})"]
    if price:
        lines.append(f"- Giá tham khảo: {price}")
    if benefits:
        lines.append(f"- Lợi ích chính: {benefits}")
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
        f"- 📞 Hotline tuyến trên: *{HOTLINE_TUYEN_TREN}*\n"
        "- 💬 Gợi ý: TVV chụp màn hình câu hỏi của khách, kèm phương án trả lời dự kiến rồi gửi cho tuyến trên để được góp ý.\n"
        "- Nếu câu hỏi liên quan đến *chính sách, hoa hồng, pháp lý*, TVV nên chuyển khách sang hotline hoặc leader phụ trách."
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
    text      = message.get("text", "")
    from_user = message.get("from", {})
    user_name = (from_user.get("first_name", "") + " " +
                 from_user.get("last_name", "")).strip() or from_user.get("username", "")

    if not text:
        send_message(chat_id, "Hiện tại em chỉ hiểu tin nhắn dạng text thôi ạ. 🙏", reply_markup=MAIN_KEYBOARD)
        return jsonify(ok=True)

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
