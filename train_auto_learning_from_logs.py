import os
import json
import re
from collections import defaultdict, Counter
from datetime import datetime

import requests
from dotenv import load_dotenv

# ========== ENV ==========
load_dotenv()

LOG_SHEET_EXPORT_URL = os.getenv("LOG_SHEET_EXPORT_URL", "")
BASE_DIR             = os.path.dirname(__file__)
DATA_DIR             = os.path.join(BASE_DIR, "data")
OUT_PATH             = os.path.join(DATA_DIR, "update_auto_learning.json")

if not LOG_SHEET_EXPORT_URL:
  raise RuntimeError("Thiếu LOG_SHEET_EXPORT_URL trong .env")


# ========== Helper ==========
def normalize_text(s: str) -> str:
    """Chuẩn hóa câu hỏi để gom nhóm: lower + bỏ dấu + gom khoảng trắng."""
    import unicodedata
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"\s+", " ", s)
    return s


def fetch_logs():
    print("[*] Fetching logs from Google Sheet (auto-learning)...")
    resp = requests.get(LOG_SHEET_EXPORT_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Lỗi khi export logs: {data}")
    return data.get("logs", [])


def parse_time_str(t):
    """
    Từ Apps Script, Time thường là dạng ISO string hoặc object serialized.
    Ở đây ta cố convert sang string để sort tương đối.
    """
    if isinstance(t, (int, float)):
        # timestamp, dùng luôn
        return t
    return str(t)


def classify_reaction(text: str) -> str:
    """
    Phân loại phản ứng của TVV với câu trả lời trước đó:
    - positive: đồng ý / khen / cảm ơn
    - negative: chê sai / không đúng / không phải
    - neutral: còn lại
    """
    t = (text or "").strip().lower()

    negative_phrases = [
        "sai rồi", "sai roi", "không đúng", "khong dung",
        "không phải", "khong phai", "nhầm rồi", "nham roi",
        "ko đúng", "ko dung", "ko phải", "ko phai",
        "chưa đúng", "chua dung", "chưa hợp lý", "chua hop ly",
        "không hợp lý", "khong hop ly",
        "tư vấn sai", "tu van sai",
        "sai combo", "sai san pham",
        "không liên quan", "khong lien quan"
    ]

    positive_phrases = [
        "đúng rồi", "dung roi", "chuẩn rồi", "chuan roi",
        "ok", "oke", "okie",
        "cảm ơn", "cam on",
        "hợp lý", "hop ly",
        "được rồi", "duoc roi",
        "tốt rồi", "tot roi",
    ]

    if any(p in t for p in negative_phrases):
        return "negative"
    if any(p in t for p in positive_phrases):
        return "positive"

    return "neutral"


def main():
    logs = fetch_logs()
    if not logs:
        print("[*] Không có log nào, dừng.")
        return

    # Gom log theo ChatID để xem chuỗi hội thoại
    conv_by_chat = defaultdict(list)
    for row in logs:
        chat_id = str(row.get("ChatID") or row.get("chat_id") or "").strip()
        if not chat_id:
            continue
        conv_by_chat[chat_id].append(row)

    # Sắp xếp theo thời gian trong từng chat
    for chat_id, arr in conv_by_chat.items():
        arr.sort(key=lambda r: parse_time_str(r.get("Time")))

    print(f"[*] Có {len(conv_by_chat)} cuộc hội thoại (chat_id) để phân tích.")

    # Thống kê mapping: (question_norm, matched_combo_id/product_code) → positive/negative
    combo_stats   = Counter()
    combo_pos_neg = defaultdict(lambda: {"positive": 0, "negative": 0})
    combo_example = {}

    prod_stats    = Counter()
    prod_pos_neg  = defaultdict(lambda: {"positive": 0, "negative": 0})
    prod_example  = {}

    # Cũng thống kê câu hỏi yếu (nhiều negative)
    weak_cases_counter = Counter()
    weak_cases_meta    = {}

    # Duyệt từng cuộc hội thoại
    for chat_id, conv in conv_by_chat.items():
        for i in range(len(conv) - 1):
            curr = conv[i]
            nxt  = conv[i + 1]

            # curr: câu TVV + Bot trả lời
            # nxt: phản ứng tiếp theo của TVV (có thể là hỏi tiếp / chê / khen)
            q_text   = str(curr.get("Text") or "")
            intent   = str(curr.get("Intent") or curr.get("intent") or "")
            combo_id = str(curr.get("MatchedComboId") or curr.get("matched_combo_id") or "").strip()
            prod_code = str(curr.get("MatchedProductCode") or curr.get("matched_product_code") or "").strip()

            if not q_text:
                continue

            reaction_text = str(nxt.get("Text") or "")
            reaction_label = classify_reaction(reaction_text)

            if reaction_label == "neutral":
                # TVV không khen/chê → chưa dùng làm nhãn
                continue

            q_norm = normalize_text(q_text)
            key_base = (q_norm, intent)

            # 1) Nếu là combo_health → học trên combo
            if intent == "combo_health" and combo_id:
                key = (q_norm, combo_id)
                if reaction_label == "positive":
                    combo_pos_neg[key]["positive"] += 1
                else:
                    combo_pos_neg[key]["negative"] += 1

                combo_stats[key] += 1
                if key not in combo_example:
                    combo_example[key] = {
                        "question_example": q_text,
                        "combo_id": combo_id,
                        "combo_name": str(curr.get("MatchedComboName") or curr.get("matched_combo_name") or "")
                    }

            # 2) Nếu là product_info / health_products → học trên sản phẩm
            if intent in ("product_info", "health_products", "product_by_code") and prod_code:
                key = (q_norm, prod_code)
                if reaction_label == "positive":
                    prod_pos_neg[key]["positive"] += 1
                else:
                    prod_pos_neg[key]["negative"] += 1

                prod_stats[key] += 1
                if key not in prod_example:
                    prod_example[key] = {
                        "question_example": q_text,
                        "product_code": prod_code,
                        "product_name": str(curr.get("MatchedProductName") or curr.get("matched_product_name") or "")
                    }

            # 3) Thống kê câu hỏi yếu (nhiều lần bị chê)
            if reaction_label == "negative":
                weak_cases_counter[key_base] += 1
                if key_base not in weak_cases_meta:
                    weak_cases_meta[key_base] = {
                        "question_example": q_text,
                        "last_intent": intent,
                    }

    # ---- Chuẩn bị output: combo_mappings + product_mappings + weak_cases ----
    combo_mappings = []
    for (q_norm, combo_id), stat in combo_stats.items():
        counts = combo_pos_neg[(q_norm, combo_id)]
        pos = counts["positive"]
        neg = counts["negative"]
        total = pos + neg
        if total == 0:
            continue

        score = (pos - neg) / total  # -1..1
        ex    = combo_example[(q_norm, combo_id)]

        combo_mappings.append({
            "question_norm": q_norm,
            "question_example": ex["question_example"],
            "intent": "combo_health",
            "combo_id": combo_id,
            "combo_name": ex["combo_name"],
            "positive": pos,
            "negative": neg,
            "score": score,
            "total_feedback": total,
            "status": "good" if score > 0.5 and total >= 2 else "weak" if score < 0 else "neutral"
        })

    product_mappings = []
    for (q_norm, prod_code), stat in prod_stats.items():
        counts = prod_pos_neg[(q_norm, prod_code)]
        pos = counts["positive"]
        neg = counts["negative"]
        total = pos + neg
        if total == 0:
            continue

        score = (pos - neg) / total
        ex    = prod_example[(q_norm, prod_code)]

        product_mappings.append({
            "question_norm": q_norm,
            "question_example": ex["question_example"],
            "intent": "product_info/health_products",
            "product_code": prod_code,
            "product_name": ex["product_name"],
            "positive": pos,
            "negative": neg,
            "score": score,
            "total_feedback": total,
            "status": "good" if score > 0.5 and total >= 2 else "weak" if score < 0 else "neutral"
        })

    # Weak cases: câu hỏi bị chê nhiều lần dù không biết combo/sản phẩm nào đúng
    weak_cases = []
    for (q_norm, intent), cnt in weak_cases_counter.items():
        meta = weak_cases_meta[(q_norm, intent)]
        weak_cases.append({
            "question_norm": q_norm,
            "question_example": meta["question_example"],
            "intent": intent,
            "negative_reactions": cnt
        })

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "from_logs_url": LOG_SHEET_EXPORT_URL,
        "stats": {
            "chat_conversations": len(conv_by_chat),
            "combo_mappings": len(combo_mappings),
            "product_mappings": len(product_mappings),
            "weak_cases": len(weak_cases),
        },
        "combo_mappings": combo_mappings,
        "product_mappings": product_mappings,
        "weak_cases": weak_cases
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("[✅] Đã sinh file auto-learning:", OUT_PATH)


if __name__ == "__main__":
    main()
