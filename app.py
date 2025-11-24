import os
import json
import re
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ===== OpenAI (dùng để hiểu intent + “mượt hóa” câu trả lời) =====
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# ===== Load ENV =====
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
HOTLINE_TUYEN_TREN = os.getenv("HOTLINE_TUYEN_TREN", "09xx.xxx.xxx")
LINK_KENH_TELEGRAM = os.getenv("LINK_KENH_TELEGRAM", "https://t.me/...")
LINK_FANPAGE = os.getenv("LINK_FANPAGE", "https://facebook.com/...")
LINK_WEBSITE = os.getenv("LINK_WEBSITE", "https://...")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOG_SHEET_WEBHOOK_URL = os.getenv("LOG_SHEET_WEBHOOK_URL", "")  # Web App Apps Script

ENABLE_AI_POLISH = os.getenv("ENABLE_AI_POLISH", "true").lower() == "true"

if not TELEGRAM_TOKEN:
    raise RuntimeError("Chưa cấu hình TELEGRAM_TOKEN trong .env")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ===== OpenAI client =====
client = None
if OPENAI_API_KEY and OpenAI is not None:
    client = OpenAI(api_key=OPENAI_API_KEY)

# ===== Load data JSON (products + combos) =====
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")

def load_loose_json(path):
    """Đọc JSON có thể bị dư dấu phẩy cuối mảng (đã gặp ở file gốc)."""
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read()
    txt = re.sub(r',\s*\]', ']', txt.strip())
    return json.loads(txt)

with open(os.path.join(DATA_DIR, "products.json"), "r", encoding="utf-8") as f:
    PRODUCTS_DATA = json.load(f)

with open(os.path.join(DATA_DIR, "combos.json"), "r", encoding="utf-8") as f:
    COMBOS_DATA = json.load(f)

PRODUCTS = PRODUCTS_DATA.get("products", [])
COMBOS = COMBOS_DATA.get("combos", [])

# Map code -> product
PRODUCT_MAP = {p["code"]: p for p in PRODUCTS if p.get("code")}

# ===== Telegram Keyboard =====
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

# ===== Flask app =====
app = Flask(__name__)

# ===== Helpers =====
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


def contains_any(text, keywords):
    text = text.lower()
    return any(k.lower() in text for k in keywords)


def find_best_combo(text):
    text = text.lower()
    best_combo = None
    score_best = 0

    for combo in COMBOS:
        keywords = combo.get("aliases", [])
        score = sum(1 for kw in keywords if kw.lower() in text)
        if score > score_best:
            score_best = score
            best_combo = combo

    return best_combo


def find_best_products(text):
    text = text.lower()
    matches = []
    for p in PRODUCTS:
        aliases = p.get("aliases", [])
        if any(a.lower() in text for a in aliases):
            matches.append(p)
    return matches


# ===== AI: phân loại intent bằng OpenAI =====
INTENT_LABELS = [
    "start",
    "buy_payment",
    "business_escalation",
    "channels",
    "combo_health",
    "product_info",
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
                        "You are an intent classifier for a Telegram bot that helps health supplement advisors.\n"
                        "Return ONLY ONE of these labels:\n"
                        f"{', '.join(INTENT_LABELS)}\n\n"
                        "Meaning:\n"
                        "- start: when user starts or greets bot\n"
                        "- buy_payment: questions about how to buy, order, pay\n"
                        "- business_escalation: hard business questions, need to connect to upline/hotline\n"
                        "- channels: asks about official channels, fanpage, website\n"
                        "- combo_health: asks which combo for a health problem (e.g. diabetes, joint pain...)\n"
                        "- product_info: asks about product, ingredients, usage, benefits\n"
                        "- menu_* : when user pressed a keyboard button with that meaning\n"
                        "- fallback: everything else\n"
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
    """Rule-based fallback + xử lý menu nút bấm."""
    t = text.lower().strip()

    # Nút menu
    if "combo theo vấn đề" in t:
        return "menu_combo"
    if "tra cứu sản phẩm" in t:
        return "menu_product_search"
    if "hướng dẫn mua hàng" in t:
        return "menu_buy_payment"
    if "kết nối tuyến trên" in t:
        return "menu_business_escalation"
    if "kênh & fanpage" in t or "kênh & fan" in t:
        return "menu_channels"

    # Lệnh hệ thống
    if t.startswith("/start") or "bắt đầu" in t or "hello" in t:
        return "start"

    # Hỏi mua hàng / thanh toán
    if contains_any(t, ["mua hàng", "đặt hàng", "đặt mua", "thanh toán", "trả tiền", "ship", "giao hàng"]):
        return "buy_payment"

    # Hỏi tuyến trên / câu hỏi khó
    if contains_any(t, ["tuyến trên", "leader", "sponsor", "upline", "khó trả lời", "hỏi giúp"]):
        return "business_escalation"

    # Hỏi kênh, fanpage, thông tin chính thức
    if contains_any(t, ["kênh", "kenh", "fanpage", "facebook", "page", "kênh chính thức"]):
        return "channels"

    # Hỏi combo / vấn đề sức khỏe
    if contains_any(t, ["tiểu đường", "đái tháo đường", "đường huyết"]) or \
       contains_any(t, ["dạ dày", "bao tử", "trào ngược", "ợ chua", "viêm loét"]) or \
       contains_any(t, ["cơ xương khớp", "đau khớp", "gout", "thoái hóa", "tim mạch", "huyết áp"]):
        return "combo_health"

    # Hỏi cụ thể về sản phẩm (mã, thành phần...)
    if contains_any(t, ["thành phần", "tác dụng", "lợi ích", "cách dùng", "công dụng", "uống như thế nào"]):
        return "product_info"

    # Thử xem có match combo hoặc sản phẩm nào không
    if find_best_combo(t) is not None:
        return "combo_health"
    if find_best_products(t):
        return "product_info"

    return "fallback"


def classify_intent(text: str):
    # 1. Thử AI trước
    label = classify_intent_ai(text)
    if label:
        return label
    # 2. Fallback rules
    return classify_intent_rules(text)


# ===== AI: “mượt hóa” câu trả lời =====
def polish_answer_with_ai(answer: str, context: dict | None = None) -> str:
    """Dùng OpenAI để viết lại câu trả lời cho mượt, nhưng KHÔNG thêm bịa đặt."""
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
        msgs = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": answer}
        ]
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0.4,
            messages=msgs
        )
        new_answer = resp.choices[0].message.content.strip()
        return new_answer or answer
    except Exception as e:
        print("Error polish_answer_with_ai:", e)
        return answer


# ===== Format answer from combos/products =====
def format_combo_answer(combo):
    name = combo.get("name", "Combo")
    header = combo.get("header_text", "")
    duration = combo.get("duration_text", "")
    products_info = []

    lines = [f"*{name}*"]
    if header:
        lines.append(f"_{header}_")
    if duration:
        lines.append(f"\n⏱ *Thời gian khuyến nghị:* {duration}")

    lines.append("\n🧩 *Các sản phẩm trong combo:*")
    for item in combo.get("products", []):
        code = item.get("product_code")
        dose = item.get("dose_text", "").strip()
        p = PRODUCT_MAP.get(code, {})
        pname = item.get("name") or p.get("name") or code
        price = item.get("price_text") or p.get("price_text", "")
        url = item.get("product_url") or p.get("product_url", "")

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
        "\n⚠️ Lưu ý: Đây là combo hỗ trợ, không thay thế thuốc điều trị. TVV nên nhắc khách tuân thủ tư vấn của bác sĩ, "
        "kết hợp chế độ ăn uống, vận động, tái khám định kỳ."
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
        name = p.get("name", "")
        code = p.get("code", "")
        ingredients = p.get("ingredients_text", "")
        usage = p.get("usage_text", "")
        benefits = p.get("benefits_text", "")
        url = p.get("product_url", "")
        price = p.get("price_text", "")

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
        "luôn nhắc khách đọc kỹ hướng dẫn sử dụng và tham khảo ý kiến bác sĩ khi cần."
    )
    return "\n".join(lines)


# ===== Các câu trả lời “menu” & cố định =====
def answer_start():
    return (
        "*Chào TVV, em là Trợ lý AI hỗ trợ kinh doanh & sản phẩm.* 🤖\n\n"
        "Anh/chị có thể:\n"
        "• Hỏi theo vấn đề sức khỏe: _\"Khách bị tiểu đường thì dùng combo nào?\"_\n"
        "• Hỏi về sản phẩm: _\"Cho em thành phần, cách dùng của ANTISWEET?\"_\n"
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
        "- \"Khách bị *huyết áp, tim mạch* thì nên dùng gì?\"\n\n"
        "Em sẽ đề xuất combo phù hợp trong danh mục hiện có."
    )


def answer_menu_product_search():
    return (
        "🔎 *Tra cứu sản phẩm*\n\n"
        "Anh/chị có thể hỏi:\n"
        "- \"Cho em info sản phẩm *ANTISWEET*?\"\n"
        "- \"Thành phần, cách dùng của *HONDROLUX* là gì?\"\n"
        "- \"Sản phẩm nào hỗ trợ *dạ dày*?\"\n\n"
        "Em sẽ trả về tên, thành phần, cách dùng, lợi ích và link sản phẩm."
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
        "- Hỏi theo dạng: \"Khách bị *tiểu đường*...\", \"Khách bị *đau dạ dày*...\", "
        "\"*Cách mua hàng*?\", \"*Thanh toán thế nào*?\", hoặc\n"
        "- Bấm nút *Kết nối tuyến trên* để em hướng dẫn liên hệ leader."
    )


# ===== Logging: gửi log lên Google Sheets (qua Apps Script Web App) =====
def log_to_sheet(payload: dict):
    if not LOG_SHEET_WEBHOOK_URL:
        return
    try:
        requests.post(
            LOG_SHEET_WEBHOOK_URL,
            json=payload,
            timeout=5
        )
    except Exception as e:
        print("Error log_to_sheet:", e)


# ===== Webhook =====
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    from_user = message.get("from", {})
    user_name = (from_user.get("first_name", "") + " " +
                 from_user.get("last_name", "")).strip() or from_user.get("username", "")

    if not text:
        send_message(chat_id, "Hiện tại em chỉ hiểu tin nhắn dạng text thôi ạ. 🙏", reply_markup=MAIN_KEYBOARD)
        return jsonify(ok=True)

    # Phân loại intent
    intent = classify_intent(text)

    # Xử lý intent và tạo reply
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
    elif intent == "combo_health":
        combo = find_best_combo(text)
        if combo:
            reply = format_combo_answer(combo)
        else:
            reply = (
                "Em chưa tìm được combo phù hợp với từ khóa anh/chị gửi. 🙏\n"
                "Anh/chị có thể ghi rõ: *tiểu đường, cơ xương khớp, tim mạch, huyết áp, tiêu hóa, gan, thận...* "
                "hoặc liên hệ tuyến trên để được hỗ trợ."
            )
    elif intent == "product_info":
        products = find_best_products(text)
        reply = format_products_answer(products)
    else:
        reply = answer_fallback()

    # Cho OpenAI “mượt hóa” câu trả lời (nếu bật)
    reply = polish_answer_with_ai(reply)

    # Gửi trả lời kèm keyboard
    send_message(chat_id, reply, reply_markup=MAIN_KEYBOARD)

    # Log lên Google Sheets
    log_payload = {
        "chat_id": chat_id,
        "user_name": user_name,
        "text": text,
        "intent": intent
        # Anh có thể bổ sung thêm trường: thời gian server, ip, v.v.
    }
    log_to_sheet(log_payload)

    return jsonify(ok=True)


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
