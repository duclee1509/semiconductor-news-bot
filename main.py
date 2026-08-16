import os
import json
import re
import datetime
import email.utils
import requests
import feedparser
import firebase_admin
from bs4 import BeautifulSoup
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse
from firebase_admin import credentials, firestore
from google import genai
from google.genai import types

from dotenv import load_dotenv
load_dotenv()

# 1. Initialize Firebase Admin from Secret
service_account_info = json.loads(os.environ.get("FIREBASE_SERVICE_ACCOUNT"))
cred = credentials.Certificate(service_account_info)
firebase_admin.initialize_app(cred)
db = firestore.client()

# 2. Initialize Gemini Client
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

RSS_SOURCES = [
    {"name": "Semiconductor Engineering", "url": "https://semiengineering.com/feed/", "category": "International", "type": "rss"},
    {"name": "IEEE Spectrum - Semiconductors", "url": "https://spectrum.ieee.org/feeds/topic/semiconductors.rss", "category": "International", "type": "rss"},
    {"name": "SemiWiki", "url": "https://semiwiki.com/feed/", "category": "International", "type": "rss"},
    {"name": "Báo Chính phủ ", "url": "https://baochinhphu.vn/ban-dan.html", "category": "VietNam", "type": "html"},
    {"name": "VnExpress", "url": "https://vnexpress.net/tag/ban-dan-236473", "category": "VietNam", "type": "html"},
    {"name": "VnExpress", "url": "https://vnexpress.net/tag/chip-346401", "category": "VietNam", "type": "html"},
    {"name": "VnExpress", "url": "https://vnexpress.net/tag/chip-9167", "category": "VietNam", "type": "html"},
    {"name": "Báo Tuổi Trẻ", "url": "https://tuoitre.vn/ban-dan.html", "category": "VietNam", "type": "html"},
    {"name": "Báo Tuổi Trẻ", "url": "https://tuoitre.vn/vi-mach.html", "category": "VietNam", "type": "html"}
]

NUMBER_OF_ARTICLES_FOR_EACH_SOURCE = 5  # Maximum number of articles to fetch from each source
MAX_LOG_FILES = 50
MAX_FAILED_ENTRIES = 100
MAX_ADDED_ENTRIES = 10000

def summarize_with_gemini(title, content, category='International'):
    if category == 'VietNam':
        prompt = f"""
        Bạn là chuyên gia biên tập tin tức công nghệ vi mạch bán dẫn.
        Hãy phân tích bài viết tiếng Việt sau:
        Tiêu đề: {title}
        Nội dung: {content}

        Yêu cầu xử lý:
        1. Tóm tắt bài viết thành 2-3 câu ngắn gọn, súc tích, nêu bật điểm cốt lõi (công nghệ, thị trường, doanh nghiệp). Giữ nguyên các thuật ngữ kỹ thuật tiếng Anh phổ biến nếu có.
        2. Phân loại xem bài viết có liên quan trực tiếp đến chính sách, quy hoạch, ưu đãi hoặc hỗ trợ từ Chính phủ/Nhà nước hay không.

        Trả về duy nhất 01 đối tượng JSON với cấu trúc:
        - "summary_org": Đoạn tóm tắt tiếng Việt (2-3 câu).
        - "isPolicy": true nếu liên quan tới chính sách/hỗ trợ nhà nước, false nếu không.
        """
    else:
        prompt = f"""
        Bạn là chuyên gia dịch thuật và biên tập tin tức vi mạch bán dẫn quốc tế.
        Hãy phân tích bài viết tiếng Anh sau:
        Tiêu đề: {title}
        Nội dung: {content}

        Yêu cầu xử lý:
        1. Dịch tiêu đề sang tiếng Việt tự nhiên, chuẩn văn phong báo chí công nghệ.
        2. Tóm tắt nội dung bài viết bằng tiếng Anh (2-3 câu súc tích).
        3. Dịch đoạn tóm tắt sang tiếng Việt (2-3 câu). 
           * Lưu ý thuật ngữ: Giữ nguyên các từ chuyên ngành tiếng Anh phổ biến (như Tape-out, Foundry, Fab, EDA, Packaging, Wafer, GAAFET, HBM, EUV, Substrate...) nếu dịch ra tiếng Việt gây khiên cưỡng hoặc mất nghĩa.
        4. Phân loại xem bài viết có liên quan trực tiếp đến chính sách, đạo luật, cấm vận hay trợ cấp chính phủ (như CHIPS Act, kiểm soát xuất khẩu...) hay không.

        Trả về duy nhất 01 đối tượng JSON với cấu trúc:
        - "title_vietnamese": Tiêu đề dịch sang tiếng Việt.
        - "summary_org": Tóm tắt gốc bằng tiếng Anh (2-3 câu).
        - "summary_vietnamese": Tóm tắt dịch sang tiếng Việt (2-3 câu).
        - "isPolicy": true nếu liên quan tới chính sách/hỗ trợ nhà nước/đạo luật, false nếu không.
        """

    response = gemini_client.models.generate_content(
        model='models/gemini-3.5-flash-lite',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )
    return json.loads(response.text)


def get_article_thumbnail(url):
    """Scrape og:image or twitter:image from the source article."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=5)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            og_image = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})

            if og_image and og_image.get('content'):
                image_url = og_image['content']
                if image_url.startswith('http'):
                    return image_url
    except Exception as e:
        print(f"Unable to fetch image from {url}: {e}")

    return None

def normalize_author_name(raw_name):
    raw_name = ' '.join(raw_name.strip().split())
    if not raw_name:
        return raw_name

    raw_name = raw_name.strip()

    lower_name = raw_name.lower()
    if ' và ' in lower_name and 'tác giả' in lower_name:
        raw_name = raw_name.split(' và ')[0].strip()

    if ' - ' in raw_name:
        raw_name = raw_name.split(' - ')[0].strip()

    duplicate_match = re.match(r'^(?P<name>.+?)\s+\1$', raw_name, re.IGNORECASE)
    if duplicate_match:
        raw_name = duplicate_match.group('name').strip()

    if raw_name.isupper():
        raw_name = raw_name.title()

    return raw_name

def parse_date_to_rfc2822(value):
    if not value:
        return None

    value = value.strip()

    if value.isdigit():
        try:
            dt = datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)
            return email.utils.format_datetime(dt)
        except Exception:
            return value

    try:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return email.utils.format_datetime(dt)
    except Exception:
        pass

    for fmt in ('%d/%m/%Y %H:%M', '%H:%M %d/%m/%Y', '%d/%m/%Y'):
        try:
            dt = datetime.datetime.strptime(value, fmt)
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            return email.utils.format_datetime(dt)
        except Exception:
            continue

    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return email.utils.format_datetime(parsed)
    except Exception:
        pass

    return value


def parse_date_to_datetime(value):
    if not value:
        return None

    value = value.strip()

    if value.isdigit():
        try:
            return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)
        except Exception:
            return None

    try:
        dt = datetime.datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        pass

    for fmt in ('%d/%m/%Y %H:%M', '%H:%M %d/%m/%Y', '%d/%m/%Y'):
        try:
            dt = datetime.datetime.strptime(value, fmt)
            return dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue

    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed
    except Exception:
        pass

    return None

def get_article_author(url):
    """Scrape author from meta tags or article HTML."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=5)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')

            if 'tuoitre.vn' in url:
                author_el = soup.select_one('div.author-item-name')
                if author_el:
                    author_text = normalize_author_name(author_el.get_text(separator=' ', strip=True))
                    if author_text:
                        return author_text

            if 'vnexpress.net' in url:
                author_el = soup.select_one('article.fck_detail p[style*="text-align:right"] strong')
                if author_el:
                    author_text = normalize_author_name(author_el.get_text(strip=True))
                    if author_text and author_text.lower() != 'vnexpress':
                        return author_text

                # Fallback: take the last right-aligned strong text in the article body.
                for fallback_el in reversed(soup.select('article.fck_detail p[style*="text-align:right"] strong')):
                    author_text = normalize_author_name(fallback_el.get_text(strip=True))
                    if author_text and author_text.lower() != 'vnexpress':
                        return author_text

            if 'baochinhphu.vn' in url:
                author_el = soup.select_one('p[style*="text-align: right"] b, p[style*="text-align:right"] b')
                if author_el:
                    author_text = normalize_author_name(author_el.get_text(strip=True))
                    if author_text and author_text.lower() not in ['baochinhphu.vn', 'báo chính phủ']:
                        return author_text

                # Fallback: any right-aligned bold text in the page.
                for fallback_el in reversed(soup.select('p[style*="text-align: right"] b, p[style*="text-align:right"] b')):
                    author_text = normalize_author_name(fallback_el.get_text(strip=True))
                    if author_text and author_text.lower() not in ['baochinhphu.vn', 'báo chính phủ']:
                        return author_text

            if 'spectrum.ieee.org' in url:
                author_el = soup.select_one('a.social-author__name')
                if author_el:
                    author_text = normalize_author_name(author_el.get_text(strip=True))
                    if author_text:
                        return author_text

                for script in soup.select('script[type="application/ld+json"]'):
                    if not script.string:
                        continue
                    try:
                        payload = json.loads(script.string)
                    except Exception:
                        continue

                    author_data = payload.get('author') if isinstance(payload, dict) else None
                    if isinstance(author_data, dict):
                        author_text = normalize_author_name(author_data.get('name', ''))
                        if author_text:
                            return author_text
                    elif isinstance(author_data, list):
                        for item in author_data:
                            if isinstance(item, dict):
                                author_text = normalize_author_name(item.get('name', ''))
                                if author_text:
                                    return author_text

            if 'eetimes.com' in url:
                page_text = soup.get_text(' ', strip=True)

                def _choose_author(matches):
                    for match in sorted(matches, key=lambda m: m.start()):
                        author_text = normalize_author_name(match.group('name'))
                        if author_text and author_text.lower() not in ['ee times staff', 'ee times']:
                            return author_text
                    return None

                bracketed_matches = list(re.finditer(
                    r'By\[(?P<name>[^\]]+)\]\([^\)]*\)\s*\d{1,2}\.\d{1,2}\.\d{4}',
                    page_text,
                    re.I
                ))
                author_text = _choose_author(bracketed_matches)
                if author_text:
                    return author_text

                plain_matches = list(re.finditer(
                    r'By\s+(?P<name>.+?)\s+\d{1,2}\.\d{1,2}\.\d{4}',
                    page_text,
                    re.I
                ))
                author_text = _choose_author(plain_matches)
                if author_text:
                    return author_text

            author_tag = (
                soup.find('meta', property='article:author')
                or soup.find('meta', attrs={'name': 'author'})
                or soup.find('meta', attrs={'name': 'twitter:creator'})
            )
            if author_tag and author_tag.get('content'):
                return author_tag['content'].strip()

            author_element = soup.find(attrs={'class': lambda value: value and 'author' in value.lower()})
            if author_element:
                return normalize_author_name(author_element.get_text(strip=True))
    except Exception as e:
        print(f"Unable to fetch author from {url}: {e}")

    return None

def get_article_published_date(entry, url=None):
    """Get published date from RSS, epoch values, or article meta tags."""
    published = parse_date_to_rfc2822(getattr(entry, 'published', None) or getattr(entry, 'updated', None))
    if published:
        return published

    if url:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                if 'baochinhphu.vn' in url:
                    published_info = soup.select_one('div.detail-time div[data-role="publishdate"]')
                    if published_info:
                        published_text = published_info.get_text(separator=' ', strip=True)
                        return parse_date_to_rfc2822(published_text)

                published_tag = (
                    soup.find('meta', property='article:published_time')
                    or soup.find('meta', property='og:article:published_time')
                    or soup.find('meta', attrs={'name': 'pubdate'})
                    or soup.find('meta', attrs={'name': 'publication_date'})
                )
                if published_tag and published_tag.get('content'):
                    published_value = published_tag['content'].strip()
                    return parse_date_to_rfc2822(published_value)
        except Exception as e:
            print(f"Unable to fetch published date from {url}: {e}")

    return None

def parse_html_source(source):
    """Fetch article list from an HTML source without an RSS feed."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(source['url'], headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('div.box-stream-item')
        if not items:
            items = soup.select('div.box-category-content')
        if not items:
            items = soup.select('div.b-grid')
        if not items:
            items = soup.select('article.item-news')

        entries = []

        for item in items:
            title_tag = item.select_one(
                'a.box-stream-link-title, a.box-category-link-title, h2.b-grid__title a, h3.b-grid__title a, h2.title-news a'
            )
            if not title_tag or not title_tag.get('href'):
                continue

            href = title_tag['href'].strip()
            article_url = urljoin(source['url'], href)
            title = title_tag.get_text(strip=True)

            summary_tag = item.select_one(
                'p.box-stream-sapo, p.box-category-sapo, div.b-grid__desc a, p.description'
            )
            summary = summary_tag.get_text(strip=True) if summary_tag else ''

            published = None
            if item.has_attr('data-publishtime'):
                raw_published = item['data-publishtime'].strip()
                if raw_published.isdigit():
                    try:
                        dt = datetime.datetime.fromtimestamp(int(raw_published), datetime.timezone.utc)
                        published = email.utils.format_datetime(dt)
                    except Exception:
                        published = raw_published
                else:
                    published = raw_published
            else:
                published_tag = item.select_one(
                    'span.time-ago.box-stream-time, span.box-category-time, span.b-grid__time'
                )
                if published_tag:
                    published = published_tag.get_text(strip=True)

            published = parse_date_to_rfc2822(published)

            entries.append(SimpleNamespace(
                title=title,
                summary=summary,
                link=article_url,
                published=published
            ))

        return entries
    except Exception as e:
        print(f"Unable to fetch article list from {source['url']}: {e}")
        return []

def get_log_datetime(filepath):
    filename = os.path.basename(filepath)
    name = filename[4:-4]

    day, month, year, hour, minute, second = name.split("_")

    return datetime.datetime(
        int(year), int(month), int(day),
        int(hour), int(minute), int(second)
    )

def run_crawler():
    news_ref = db.collection('semiconductor_news')
    added_count = 0
    failed_file = 'failed_article.json'
    failed_entries = []
    failed_urls = set()
    log_dir = 'log'
    os.makedirs(log_dir, exist_ok=True)
    now = datetime.datetime.now()
    log_file = os.path.join(log_dir, f"log_{now.day}_{now.month}_{now.year}_{now.hour}_{now.minute}_{now.second}.txt")

    # Remove old log files if exceeding MAX_LOG_FILES
    log_files = [
        os.path.join(log_dir, f)
        for f in os.listdir(log_dir)
        if f.startswith("log_") and f.endswith(".txt")
    ]
    log_files.sort(key=get_log_datetime)
    while len(log_files) > MAX_LOG_FILES:
        old_file = log_files.pop(0)
        os.remove(old_file)

    added_file = 'added_article.json'
    added_entries = []
    added_urls = set()

    if os.path.exists(failed_file):
        try:
            with open(failed_file, 'r', encoding='utf-8') as f:
                failed_entries = json.load(f)
                failed_urls = {item.get('article_url') for item in failed_entries if item.get('article_url')}
        except Exception as e:
            print(f"Unable to read {failed_file}: {e}")
            failed_entries = []
            failed_urls = set()

    if os.path.exists(added_file):
        try:
            with open(added_file, 'r', encoding='utf-8') as f:
                added_entries = json.load(f)
                added_urls = {item.get('url') for item in added_entries if item.get('url')}
        except Exception as e:
            print(f"Unable to read {added_file}: {e}")
            added_entries = []
            added_urls = set()

    def add_failed_entry(entry_data):
        nonlocal failed_entries, failed_urls
        article_url = entry_data.get('article_url')
        if not article_url or article_url in failed_urls:
            return

        failed_entries.append(entry_data)
        failed_urls.add(article_url)
        if len(failed_entries) > MAX_FAILED_ENTRIES:
            removed = failed_entries.pop(0)
            failed_urls.discard(removed.get('article_url'))

        try:
            with open(failed_file, 'w', encoding='utf-8') as f:
                json.dump(failed_entries, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Unable to write to {failed_file}: {e}")

    def add_added_entry(entry_data):
        nonlocal added_entries, added_urls
        article_url = entry_data.get('url')
        if not article_url or article_url in added_urls:
            return

        added_entries.append(entry_data)
        added_urls.add(article_url)
        if len(added_entries) > MAX_ADDED_ENTRIES:
            removed = added_entries.pop(0)
            removed_url = removed.get('url')
            added_urls.discard(removed_url)
            if removed_url:
                try:
                    docs = news_ref.where('url', '==', removed_url).get()
                    for doc in docs:
                        news_ref.document(doc.id).delete()
                except Exception as e:
                    print(f"Unable to delete stale Firebase entry for {removed_url}: {e}")

        try:
            with open(added_file, 'w', encoding='utf-8') as f:
                json.dump(added_entries, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Unable to write to {added_file}: {e}")

    for source in RSS_SOURCES:
        if source.get('type') == 'html':
            entries = parse_html_source(source)
        else:
            feed = feedparser.parse(source["url"])
            entries = feed.entries[:NUMBER_OF_ARTICLES_FOR_EACH_SOURCE]

        source_hostname = urlparse(source['url']).hostname
        if source_hostname and source_hostname.startswith('www.'):
            source_hostname = source_hostname[4:]

        for entry in entries[:NUMBER_OF_ARTICLES_FOR_EACH_SOURCE]:
            article_url = entry.link
            if article_url in added_urls:
                continue

            article_hostname = urlparse(article_url).hostname
            if article_hostname and article_hostname.startswith('www.'):
                article_hostname = article_hostname[4:]

            try:
                raw_title = entry.title
            except Exception:
                raw_title = None
            if raw_title and raw_title.startswith("Tin tức sáng"):
                continue

            try:
                raw_summary = getattr(entry, 'summary', getattr(entry, 'description', ''))
            except Exception:
                raw_summary = None
            # Preview only the first line of the summary and truncate to 200 chars
            if raw_summary:
                summary_preview = raw_summary.splitlines()[0].strip()
                if len(summary_preview) > 200:
                    summary_preview = summary_preview[:197] + '...'
            else:
                summary_preview = ''

            # attempt to fetch thumbnail/author/published with exception safety
            try:
                thumbnail_url = get_article_thumbnail(article_url)
            except Exception:
                thumbnail_url = None

            try:
                author_name = get_article_author(article_url)
            except Exception:
                author_name = None

            try:
                published_date = get_article_published_date(entry, article_url)
            except Exception:
                published_date = None

            # If any critical piece is missing, log full info and skip
            if (
                source_hostname and article_hostname and source_hostname not in article_hostname
            ) or not author_name or not published_date or not raw_title or not raw_summary:
                add_failed_entry({
                    'article_url': article_url,
                    'title': raw_title,
                    'summary': summary_preview,
                    'thumbnail': thumbnail_url,
                    'author': author_name,
                    'published_date': published_date
                })
                continue

            print(f"Article: {article_url}")
            print(f"Title: {raw_title}")
            print(f"Summary: {summary_preview}")
            print(f"Thumbnail: {thumbnail_url}")
            print(f"Author: {author_name}")
            print(f"Published date: {published_date}")
            print("")

            try:
                with open(log_file, 'a', encoding='utf-8') as logf:
                    logf.write(f"Article: {article_url}\n")
                    logf.write(f"Title: {raw_title}\n")
                    logf.write(f"Summary: {summary_preview}\n")
                    logf.write(f"Thumbnail: {thumbnail_url}\n")
                    logf.write(f"Author: {author_name}\n")
                    logf.write(f"Published date: {published_date}\n")
                    logf.write("\n")
            except Exception as e:
                print(f"Unable to write log to {log_file}: {e}")

            try:
                ai_result = summarize_with_gemini(raw_title, raw_summary, category=source['category'])
                # ai_result = {
                #     'title_vietnamese': '',
                #     'summary_org': raw_summary,
                #     'summary_vietnamese': '',
                #     'isPolicy': False
                # }
                title_vietnamese = ''
                summary_vietnamese = ''
                if source['category'] == 'International':
                    title_vietnamese = ai_result.get('title_vietnamese', '')
                    summary_vietnamese = ai_result.get('summary_vietnamese', '')
            
                published_at_dt = parse_date_to_datetime(published_date)

                doc_data = {
                    'title_org': raw_title,
                    'title_vietnamese': title_vietnamese,
                    'summary_org': ai_result.get('summary_org', ''),
                    'summary_vietnamese': summary_vietnamese,
                    'source': source['name'],
                    'url': article_url,
                    'category': source['category'],
                    'isPolicy': ai_result.get('isPolicy', False),
                    'thumbnail': thumbnail_url,
                    'author': author_name,
                    'createdAt': firestore.SERVER_TIMESTAMP,
                    'publishedAt': published_at_dt if published_at_dt else None
                }

                added_entry_data = doc_data.copy()
                added_entry_data['createdAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                added_entry_data['publishedAt'] = published_at_dt.isoformat() if published_at_dt else None

                news_ref.add(doc_data)
                added_count += 1
                add_added_entry(added_entry_data)
                print(f"Added: {raw_title}")
            except Exception as e:
                print(f"Error processing {article_url}: {str(e)}")
                continue

    completion_msg = f"Completed! Added a total of {added_count} new news items to Firebase."
    print(completion_msg)
    try:
        with open(log_file, 'a', encoding='utf-8') as logf:
            logf.write(completion_msg + "\n")
    except Exception as e:
        print(f"Unable to write completion message to {log_file}: {e}")

if __name__ == "__main__":
    run_crawler()
