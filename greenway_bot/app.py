import os
import json
import requests

from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ===== Load ENV =====
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
HOTLINE_TUYEN_TREN = os.getenv("HOTLINE_TUYEN_TREN", "09xx.xxx.xxx")
LINK_KENH_TELEGRAM = os.getenv("LINK_KENH_TELEGRAM", "https://t.me/...")
LINK_FANPAGE = os.getenv("LINK_FANPAGE", "https://facebook.com/...")
LINK_WEBSITE = os.getenv("LINK_WEBSITE", "https://...")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Chưa cấu hình TELEGRAM_TOKEN trong .env")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ===== Load data JSON =====
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

with open(os.path.join(DATA_DIR, "combos.json"), "r", encoding="utf-8") as f:
    COMBOS_DATA = json.load(f)

with open(os.path.join(DATA_DIR, "products.json"), "r", encoding="utf-8") as f:
    PRODUCTS_DATA = json.load(f)

COMBOS = COMBOS_DATA.get("combos", [])
PRODUCTS = PRODUCTS_DATA.get("products", [])

# Tạo map product_code -> product
PRODUCT_MAP = {p["code"]: p for p in PRODUCTS}

app = Flask(__name__)

# ===== Helpers =====
def send_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)

    url = f"{TELEGRAM_API}/sendMessage"
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Error sending message:", e)


def contains_any(text, keywords):
    text = text.lower()
    return any(k.lower() in text for k in keywords)


def classify_intent(text):
    t = text.lower().strip()

    # Lệnh hệ thống
    if t.startswith("/start") or "bắt đầu" in t:
        return "start"

    # Hỏi mua hàng / thanh toán
    if contains_any(t, ["mua hàng", "đặt hàng", "đặt mua", "thanh toán", "trả tiền", "ship", "giao hàng"]):
        return "buy_payment"

    # Hỏi tuyến trên / câu hỏi khó
    if contains_any(t, ["tuyến trên", "leader", "sponsor", "upline", "khó trả lời", "hỏi giúp", "chai câu"]):
        return "business_escalation"

    # Hỏi kênh, fanpage, thông tin chính thức
    if contains_any(t, ["kênh", "kenh", "fanpage", "facebook", "page", "kênh chính thức", "zalo official"]):
        return "channels"

    # Hỏi combo / vấn đề sức khỏe (tiểu đường, dạ dày,...)
    if contains_any(t, ["tiểu đường", "đái tháo đường", "đường huyết"]) or \
       contains_any(t, ["dạ dày", "bao tử", "trào ngược", "ợ chua", "viêm loét"]):
        return "combo_health"

    # Hỏi cụ thể về sản phẩm (mã sản phẩm, tên, thành phần...)
    if contains_any(t, ["thành phần", "tác dụng", "lợi ích", "cách dùng", "công dụng", "uống như thế nào"]):
        return "product_info"

    # Thử xem có match combo hoặc sản phẩm nào không
    if find_best_combo(t) is not None:
        return "combo_health"
    if find_best_products(t):
        return "product_info"

    # Mặc định
    return "fallback"


def find_best_combo(text):
    text = text.lower()
    best_combo = None
    score_best = 0

    for combo in COMBOS:
        keywords = combo.get("keywords", [])
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


def format_combo_answer(combo):
    name = combo.get("name", "Combo")
    header = combo.get("header_text", "")
    duration = combo.get("duration_text", "")
    usage = combo.get("usage_text", "")
    note = combo.get("note_text", "")
    combo_url = combo.get("combo_url", "")

    lines = []
    lines.append(f"*{name}*")
    if header:
        lines.append(f"_{header}_")
    if duration:
        lines.append(f"\n⏱ *Thời gian khuyến nghị:* {duration}")
    if usage:
        lines.append(f"\n💊 *Cách dùng tổng quan:* {usage}")

    # Liệt kê từng sản phẩm trong combo
    products_info = []
    for item in combo.get("products", []):
        code = item.get("product_code")
        dose = item.get("dose_text", "")
        note_item = item.get("optional_note", "")

        p = PRODUCT_MAP.get(code)
        if not p:
            continue

        line = f"• *{p.get('name', code)}* ({code})"
        if dose:
            line += f"\n  - Liều dùng: {dose}"
        if note_item:
            line += f"\n  - Ghi chú: {note_item}"
        url = p.get("product_url")
        if url:
            line += f"\n  - 🔗 Link sản phẩm: {url}"
        products_info.append(line)

    if products_info:
        lines.append("\n\n🧩 *Các sản phẩm trong combo:*")
        lines.append("\n".join(products_info))

    if combo_url:
        lines.append(f"\n🌐 Link combo trên web: {combo_url}")
    if note:
        lines.append(f"\n⚠️ *Lưu ý:* {note}")

    lines.append("\n👉 TVV nên hỏi thêm tình trạng cụ thể của khách để tư vấn cá nhân hóa hơn.")

    return "\n".join(lines)


def format_products_answer(products):
    if not products:
        return "Em chưa tìm được sản phẩm phù hợp trong danh mục hiện có ạ. Anh/chị có thể mô tả rõ hơn tình trạng khách giúp em nhé."

    lines = []
    lines.append("Dưới đây là *một số sản phẩm phù hợp* trong danh mục hiện tại:\n")

    for p in products[:5]:
        name = p.get("name", "")
        code = p.get("code", "")
        ingredients = p.get("ingredients_text", "")
        usage = p.get("usage_text", "")
        benefits = p.get("benefits_text", "")
        url = p.get("product_url", "")

        block = f"*{name}* ({code})"
        if benefits:
            block += f"\n- Lợi ích chính: {benefits}"
        if ingredients:
            block += f"\n- Thành phần nổi bật: {ingredients}"
        if usage:
            block += f"\n- Cách dùng gợi ý: {usage}"
        if url:
            block += f"\n- 🔗 Link sản phẩm: {url}"

        lines.append(block)
        lines.append("")  # dòng trống

    lines.append("👉 TVV hãy chọn sản phẩm phù hợp nhất với tình trạng cụ thể của khách và chính sách hiện hành của công ty.")
    return "\n".join(lines)


def answer_buy_payment():
    lines = []
    lines.append("*Hướng dẫn mua hàng & thanh toán* 🛒")
    lines.append("\n1️⃣ *Cách mua hàng:*")
    lines.append(f"- Đặt trực tiếp trên website: {LINK_WEBSITE}")
    lines.append("- Nhờ TVV tạo đơn hàng trên hệ thống.")
    lines.append("- Gọi Hotline để được hỗ trợ tạo đơn.")

    lines.append("\n2️⃣ *Các bước đặt trên website (gợi ý):*")
    lines.append("   1. Truy cập website.")
    lines.append("   2. Chọn sản phẩm → bấm *Thêm vào giỏ*.")
    lines.append("   3. Vào *Giỏ hàng* → kiểm tra sản phẩm.")
    lines.append("   4. Bấm *Thanh toán* → nhập thông tin nhận hàng.")
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
        "👉 TVV nên ưu tiên gửi cho khách các đường link chính thức này để đảm bảo thông tin chuẩn."
    )


def answer_start():
    text = (
        "*Chào TVV, em là Trợ lý AI hỗ trợ kinh doanh & sản phẩm.* 🤖\n\n"
        "Anh/chị có thể hỏi em:\n"
        "• \"Khách bị *tiểu đường* thì dùng combo nào?\"\n"
        "• \"Người bị *đau dạ dày* nên dùng sản phẩm gì?\"\n"
        "• \"Cách *mua hàng / thanh toán* như thế nào?\"\n"
        "• \"Câu này em *khó trả lời*, nhờ tuyến trên hỗ trợ?\"\n"
        "• \"Cho xin *kênh, fanpage* chính thức?\"\n\n"
        "Em sẽ cố gắng trả lời trong phạm vi dữ liệu công ty đã cung cấp. ❤️"
    )
    return text


def answer_fallback():
    return (
        "Hiện tại em chưa hiểu rõ câu hỏi hoặc chưa có dữ liệu cho nội dung này ạ. 🙏\n\n"
        "Anh/chị có thể:\n"
        "- Mô tả *cụ thể hơn* tình trạng của khách, hoặc\n"
        "- Dùng các câu kiểu: \"Khách bị *tiểu đường*...\", \"Khách bị *đau dạ dày*...\", "
        "\"*Cách mua hàng*?\", \"*Thanh toán thế nào*?\", hoặc\n"
        "- Gõ: *tuyến trên* để em hướng dẫn kết nối leader hỗ trợ."
    )


# ===== Webhook route =====
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    # Debug:
    # print(json.dumps(update, ensure_ascii=False, indent=2))

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if not text:
        send_message(chat_id, "Hiện tại em chỉ hiểu tin nhắn dạng text thôi ạ. 🙏")
        return jsonify(ok=True)

    intent = classify_intent(text)

    if intent == "start":
        reply = answer_start()
    elif intent == "buy_payment":
        reply = answer_buy_payment()
    elif intent == "business_escalation":
        reply = answer_business_escalation()
    elif intent == "channels":
        reply = answer_channels()
    elif intent == "combo_health":
        combo = find_best_combo(text)
        if combo:
            reply = format_combo_answer(combo)
        else:
            reply = (
                "Em chưa tìm được combo phù hợp với từ khóa anh/chị gửi. 🙏\n"
                "Anh/chị có thể ghi rõ: *tiểu đường, dạ dày, xương khớp, tim mạch,...* "
                "hoặc liên hệ tuyến trên để được hỗ trợ."
            )
    elif intent == "product_info":
        products = find_best_products(text)
        reply = format_products_answer(products)
    else:
        reply = answer_fallback()

    send_message(chat_id, reply)
    return jsonify(ok=True)


@app.route("/healthz", methods=["GET"])
def healthz():
    return "ok", 200


if __name__ == "__main__":
    # Chạy local để test
    app.run(host="0.0.0.0", port=8000, debug=True)
