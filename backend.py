# backend.py - With retry logic and rate limit handling
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
import time
import google.generativeai as genai
import os

app = Flask(__name__)
CORS(app)

class RBIScraper:
    # ... (same as before, no changes needed)
    def __init__(self):
        self.base_url = "https://rbi.org.in"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        }

    def get_notification_list(self, limit=15):
        url = f"{self.base_url}/Scripts/NotificationUser.aspx"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            notifications = []
            content_area = soup.find('div', id='content') or soup.find('div', class_='content') or soup
            links = content_area.find_all('a', href=True)
            for link in links:
                href = link['href']
                if 'NotificationUser.aspx?Id=' in href:
                    match = re.search(r'Id=(\d+)', href)
                    if match:
                        notif_id = match.group(1)
                        title = link.get_text(strip=True)
                        if title and len(title) > 5:
                            parent_td = link.find_parent('td')
                            date_text = ""
                            if parent_td:
                                next_td = parent_td.find_next_sibling('td')
                                if next_td:
                                    date_text = next_td.get_text(strip=True)
                            notifications.append({
                                'id': notif_id,
                                'title': title,
                                'date': date_text,
                                'url': f"{self.base_url}/Scripts/NotificationUser.aspx?Id={notif_id}&Mode=0"
                            })
                        if len(notifications) >= limit:
                            break
            return notifications
        except Exception as e:
            print(f"Error fetching list: {e}")
            return []

    def scrape_notification(self, notif_id):
        url = f"{self.base_url}/Scripts/NotificationUser.aspx?Id={notif_id}&Mode=0"
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            text = response.text
            ref_match = re.search(r'RBI/\d{4}-\d{2}/\d{2,3}', text)
            reference = ref_match.group(0) if ref_match else "Not specified"
            date_match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}', text)
            date_published = date_match.group(0) if date_match else ""
            title_tag = soup.find('title')
            title = title_tag.get_text(strip=True) if title_tag else "RBI Notification"
            content_div = soup.find('div', id='content') or soup.find('div', class_='content') or soup.find('body')
            paragraphs = []
            if content_div:
                for p in content_div.find_all(['p', 'div', 'span']):
                    para_text = p.get_text(strip=True)
                    if len(para_text) > 50 and not para_text.startswith('http'):
                        paragraphs.append(para_text)
            content = '\n\n'.join(paragraphs[:30])
            return {
                'id': notif_id,
                'title': title,
                'reference': reference,
                'date_published': date_published,
                'content': content[:8000]
            }
        except Exception as e:
            print(f"Error scraping {notif_id}: {e}")
            return None

scraper = RBIScraper()

# Helper function to call Gemini with retry
def call_gemini_with_retry(api_key, prompt, max_retries=3, base_delay=5):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    for attempt in range(max_retries):
        try:
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            error_str = str(e)
            if '429' in error_str or 'quota' in error_str.lower():
                # Extract wait time from error message if possible
                wait_time = base_delay * (2 ** attempt)  # exponential backoff
                import re
                match = re.search(r'retry in (\d+(?:\.\d+)?)\s*seconds', error_str, re.IGNORECASE)
                if match:
                    wait_time = float(match.group(1))
                print(f"Rate limit hit, waiting {wait_time} seconds before retry {attempt+1}/{max_retries}")
                time.sleep(wait_time)
                continue
            else:
                raise e
    raise Exception("Gemini API rate limit exceeded after multiple retries. Please wait a minute and try again.")

# API Routes
@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    limit = request.args.get('limit', 15, type=int)
    notifications = scraper.get_notification_list(limit)
    return jsonify({'success': True, 'notifications': notifications})

@app.route('/api/notification/<notif_id>', methods=['GET'])
def get_notification(notif_id):
    data = scraper.scrape_notification(notif_id)
    if data:
        return jsonify({'success': True, 'data': data})
    else:
        return jsonify({'success': False, 'error': 'Could not fetch notification'}), 404

@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json
    content = data.get('content', '')
    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'success': False, 'error': 'Gemini API key required'}), 400
    try:
        prompt = f"""
        You are a compliance expert. Analyze this RBI notification and provide:
        1. SUMMARY: 5 bullet points of key requirements
        2. RISKS: Top 3 compliance risks (label each as High/Medium/Low)
        3. ACTIONS: Immediate and long-term actions needed
        4. DEADLINES: Any specific deadlines mentioned
        Regulation text:
        {content[:6000]}
        """
        analysis = call_gemini_with_retry(api_key, prompt)
        return jsonify({'success': True, 'analysis': analysis})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 429 if 'rate limit' in str(e).lower() else 500

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    question = data.get('question', '')
    context = data.get('context', '')
    api_key = data.get('api_key', '')
    if not api_key:
        return jsonify({'success': False, 'error': 'Gemini API key required'}), 400
    try:
        prompt = f"""
        You are a compliance expert. Based on this RBI notification:
        {context[:5000]}
        Question: {question}
        Provide a clear, actionable answer. Include specific requirements, deadlines, or penalties if mentioned.
        """
        answer = call_gemini_with_retry(api_key, prompt)
        return jsonify({'success': True, 'answer': answer})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 429 if 'rate limit' in str(e).lower() else 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)