import os
import json
import datetime
import feedparser
import firebase_admin
from firebase_admin import credentials, firestore
from google import genai
from google.genai import types

# 1. Khởi tạo Firebase Admin từ Secret
service_account_info = json.loads(os.environ.get("FIREBASE_SERVICE_ACCOUNT"))
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 2. Khởi tạo Gemini Client
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

RSS_SOURCES = [
    {"name": "EETimes", "url": "https://www.eetimes.com/feed/", "category": "International"},
    {"name": "Semiconductor Engineering", "url": "https://semiengineering.com/feed/", "category": "International"}
]

def summarize_with_gemini(title, content):
    prompt = f"""
    Bạn là chuyên gia biên tập tin tức vi mạch bán dẫn. Hãy phân tích bài viết sau:
    Tiêu đề: {title}
    Nội dung: {content}

    Yêu cầu trả về định dạng JSON duy nhất với các trường:
    - title: Tiêu đề dịch sang tiếng Việt gọn gàng, hấp dẫn.
    - summary: Tóm tắt 2-3 câu ngắn gọn bằng tiếng Việt, nêu rõ điểm cốt lõi.
    - isPolicy: true nếu bài viết liên quan trực tiếp tới chính sách/hỗ trợ nhà nước, false nếu không.
    """
    
    response = gemini_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )
    return json.loads(response.text)

def run_crawler():
    news_ref = db.collection('semiconductor_news')
    added_count = 0

    for source in RSS_SOURCES:
        feed = feedparser.parse(source["url"])
        
        for entry in feed.entries[:3]:
            article_url = entry.link
            
            # Kiểm tra trùng lặp
            existing = news_ref.where('url', '==', article_url).limit(1).get()
            if len(existing) > 0:
                continue

            raw_title = entry.title
            raw_summary = getattr(entry, 'summary', getattr(entry, 'description', ''))

            try:
                ai_result = summarize_with_gemini(raw_title, raw_summary)
                
                doc_data = {
                    'title': ai_result.get('title', raw_title),
                    'summary': ai_result.get('summary', ''),
                    'source': source['name'],
                    'url': article_url,
                    'category': source['category'],
                    'isPolicy': ai_result.get('isPolicy', False),
                    'createdAt': firestore.SERVER_TIMESTAMP,
                    'publishedAt': entry.get('published', datetime.datetime.now().isoformat())
                }
                
                news_ref.add(doc_data)
                added_count += 1
                print(f"Đã thêm: {ai_result.get('title')}")

            except Exception as e:
                print(f"Lỗi khi xử lý {article_url}: {str(e)}")
                continue

    print(f"Hoàn tất! Đã thêm tổng cộng {added_count} tin tức mới.")

if __name__ == "__main__":
    run_crawler()
