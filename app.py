from flask import Flask, render_template, render_template_string, jsonify, request
import imaplib
import email
from email.header import decode_header
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import time
import threading
import os
import google.generativeai as genai
from dotenv import load_dotenv
from pymongo import MongoClient
from datetime import datetime, timedelta
import re
from langdetect import detect, LangDetectException

load_dotenv()

app = Flask(__name__)

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGODB_URI = os.getenv("DATABASE_URL")

DEPARTMENTS = {
    "Technical": os.getenv("TECH_EMAIL"),
    "Billing": os.getenv("BILLING_EMAIL"),
    "Complaint": os.getenv("COMPLAINT_EMAIL"),
    "General Inquiry": os.getenv("GENERAL_EMAIL"),
}

# Define priority keywords
URGENT_KEYWORDS = [
    "urgent", "asap", "immediately", "emergency", "critical", 
    "important", "deadline", "quick", "expedite", "rush"
]

client = MongoClient(MONGODB_URI)
db = client.emails_db
emails_collection = db.emails
responses_collection = db.responses

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

polling_active = False

def classify_email(body):
    prompt = f"""
    Classify the issue below into one of these categories:
    - Technical
    - Billing
    - Complaint
    - General Inquiry

    Message: "{body}"
    Only return the category.
    """
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Classification Error:", e)
        return "Unclassified"

def analyze_sentiment(body):
    """Analyze the sentiment of the email body."""
    prompt = f"""
    Analyze the sentiment of this message and classify it as one of:
    - Positive
    - Neutral
    - Negative
    - Very Negative
    
    Message: "{body}"
    
    Only return the sentiment category.
    """
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Sentiment Analysis Error:", e)
        return "Neutral"

def calculate_priority(subject, body, sentiment):
    """Calculate priority score (1-5) based on content and sentiment."""
    priority = 3  # Default - medium priority
    
    # Check for urgent keywords
    subject_lower = subject.lower()
    body_lower = body.lower()
    
    for keyword in URGENT_KEYWORDS:
        if keyword in subject_lower or keyword in body_lower:
            priority += 1
            break
    
    # Adjust based on sentiment
    if sentiment == "Very Negative":
        priority += 1
    elif sentiment == "Negative":
        priority += 0.5
    elif sentiment == "Positive":
        priority -= 0.5
    
    return min(max(int(priority), 1), 5)

def detect_language(text):
    """Detect the language of the email."""
    try:
        return detect(text)
    except LangDetectException:
        return "en"  # Default to English if detection fails

def generate_auto_response(body, category, sentiment):
    """Generate an AI-powered auto-response for common inquiries."""
    prompt = f"""
    Generate a professional, helpful email response to this customer inquiry.
    
    Category: {category}
    Customer sentiment: {sentiment}
    Customer message: "{body}"
    
    The response should:
    1. Acknowledge their inquiry
    2. Provide helpful initial information
    3. Set expectations for follow-up if needed
    4. Be concise (3-5 sentences maximum)
    5. Have a professional but warm tone
    
    Only return the response text, nothing else.
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Auto-response Generation Error:", e)
        return None

def summarize_email(body):
    """Generate a concise summary of a long email."""
    if len(body.split()) < 100:  # Don't summarize short emails
        return body
        
    prompt = f"""
    Summarize this email in 2-3 sentences while preserving the key points and any specific requests:
    
    "{body}"
    
    Only return the summary, nothing else.
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Email Summarization Error:", e)
        return body[:300] + "..." if len(body) > 300 else body

def extract_customer_id(body):
    customer_id_patterns = [
        r'customer\s*(?:id|ID|number|#|No)[:.\s]*([A-Z0-9]{4,15})',
        r'account\s*(?:id|ID|number|#|No)[:.\s]*([A-Z0-9]{4,15})',
        r'client\s*(?:id|ID|number|#|No)[:.\s]*([A-Z0-9]{4,15})',
        r'user\s*(?:id|ID|number|#|No)[:.\s]*([A-Z0-9]{4,15})',
        
        r'ref(?:erence)?\s*(?:id|ID|number|#|No)?[:.\s]*([A-Z0-9]{4,15})',
        r'order\s*(?:id|ID|number|#|No)[:.\s]*([A-Z0-9]{4,15})',
        r'(?:id|ID|#|No)[:.\s]*([A-Z0-9]{4,15})',
        
        r'#\s*([A-Z0-9]{4,15})',
        r'\b(CUS[A-Z0-9]{5,12})\b',
        r'\b(ACC[A-Z0-9]{5,12})\b',
        r'\b(ID[A-Z0-9]{5,12})\b',
        
        r'my (?:customer|account|client|reference)? (?:id|ID|number) is[:\s]+([A-Z0-9]{4,15})',
        r'using (?:customer|account|client|reference)? (?:id|ID|number)[:\s]+([A-Z0-9]{4,15})'
    ]
    
    for pattern in customer_id_patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            # Clean up any trailing punctuation
            customer_id = match.group(1).strip()
            if customer_id.endswith(('.', ',', ':', ';', ')', ']', '}')):
                customer_id = customer_id[:-1]
            return customer_id
    
    try:
        prompt = f"""
        Extract the customer ID or account number from this email if present. 
        Only respond with the ID/number itself, nothing else.
        If no customer ID is found, respond with "None".
        
        Email: "{body[:1000]}"  # Limit length for API efficiency
        """
        
        response = model.generate_content(prompt)
        extracted_id = response.text.strip()
        
        # Only return if it looks like a valid ID
        if extracted_id != "None" and re.match(r'^[A-Z0-9]{4,15}$', extracted_id):
            return extracted_id
    except Exception as e:
        print(f"AI extraction error: {str(e)}")
    
    return None

def send_auto_response(to_email, auto_response, subject):
    """Send an automatic acknowledgment to the customer."""
    try:
        msg = MIMEMultipart('alternative')
        msg["Subject"] = f"RE: {subject} [Automated Acknowledgment]"
        msg["From"] = EMAIL_USER
        msg["To"] = to_email
        
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 5px;">
                <div style="border-bottom: 2px solid #4285F4; padding-bottom: 10px; margin-bottom: 20px;">
                    <h2 style="color: #4285F4; margin: 0;">Thank you for your message</h2>
                </div>
                
                <p>Dear Valued Customer,</p>
                
                <p>{auto_response}</p>
                
                <p style="margin-top: 30px;">Best regards,<br>
                Smart Email Management System<br>
                Team Wanderers</p>
                
                <div style="margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; font-size: 12px; color: #777;">
                    <p>This is an automated response. Please do not reply directly to this email.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        text_content = f"""
        Thank you for your message.
        
        {auto_response}
        
        Best regards,
        Smart Email Management System
        Team Wanderers
        
        Note: This is an automated response. Please do not reply directly to this email.
        """
        
        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')
        msg.attach(part1)
        msg.attach(part2)
        
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
            print(f"Auto-response sent to {to_email}")
            return True
    except Exception as e:
        print(f"Auto-response error: {str(e)}")
        return False

def create_email_template(subject, body, category, sender, sentiment="", priority=3, summary="", language="en", customer_id=None):
    """Create a modern HTML email template with enhanced metadata."""
    current_date = datetime.now().strftime("%B %d, %Y")
    
    priority_colors = {
        1: "#34a853",  # Green (low)
        2: "#8abb6f",  # Light green
        3: "#fbbc05",  # Yellow (medium)
        4: "#ff914d",  # Orange
        5: "#ea4335"   # Red (high)
    }
    
    priority_text = {
        1: "Low",
        2: "Low-Medium",
        3: "Medium",
        4: "Medium-High",
        5: "High"
    }
    
    sentiment_colors = {
        "Positive": "#34a853",     # Green
        "Neutral": "#fbbc05",      # Yellow
        "Negative": "#ff914d",     # Orange
        "Very Negative": "#ea4335" # Red
    }
    
    html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Forwarded Inquiry</title>
        <style>
            body {{
                font-family: 'Segoe UI', Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 650px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background-color: #4285F4;
                color: white;
                padding: 20px;
                border-radius: 5px 5px 0 0;
                text-align: center;
            }}
            .content {{
                padding: 20px;
                background-color: #f9f9f9;
                border: 1px solid #ddd;
                border-top: none;
                border-radius: 0 0 5px 5px;
            }}
            .footer {{
                margin-top: 20px;
                font-size: 12px;
                color: #777;
                text-align: center;
            }}
            .category-badge {{
                display: inline-block;
                background-color: #34a853;
                color: white;
                padding: 5px 10px;
                border-radius: 3px;
                font-size: 14px;
                margin-bottom: 15px;
            }}
            .message-body {{
                border-left: 3px solid #4285F4;
                padding-left: 15px;
                margin: 15px 0;
                white-space: pre-line;
            }}
            .sender-info {{
                background-color: #f2f2f2;
                padding: 10px;
                border-radius: 5px;
                margin-bottom: 15px;
            }}
            .sender-email {{
                color: #4285F4;
                font-weight: bold;
            }}
            .reply-button {{
                display: inline-block;
                background-color: #4285F4;
                color: white;
                padding: 8px 15px;
                text-decoration: none;
                border-radius: 4px;
                margin-top: 5px;
            }}
            .metadata {{
                display: flex;
                flex-wrap: wrap;
                margin: 10px 0;
                gap: 10px;
            }}
            .metadata-item {{
                padding: 5px 10px;
                border-radius: 3px;
                font-size: 13px;
                color: white;
            }}
            .summary-box {{
                background-color: #f8f9fa;
                border: 1px solid #dce0e3;
                border-radius: 5px;
                padding: 10px;
                margin: 10px 0;
                font-style: italic;
            }}
            .customer-id {{
                display: inline-block;
                background-color: #4285F4;
                color: white;
                padding: 3px 8px;
                border-radius: 15px;
                font-size: 12px;
                margin-left: 10px;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2>Customer Inquiry</h2>
        </div>
        <div class="content">
            <span class="category-badge">{category}</span>
            
            <div class="sender-info">
                <strong>From:</strong> <span class="sender-email">{sender}</span>
                {f'<span class="customer-id">Customer ID: {customer_id}</span>' if customer_id else ''}
                <br>
                <a href="mailto:{sender}" class="reply-button">Reply to Customer</a>
            </div>
            <h3>Subject: {subject}</h3>
            
            {f'<div class="summary-box"><strong>Summary:</strong> {summary}</div>' if summary and summary != body else ''}
            
            <p>The following message has been automatically categorized and forwarded by our AI-powered Smart Email Management System on {current_date}.</p>
            
            <div class="message-body">
                {body}
            </div>
            
            <p>Please handle this inquiry according to our standard procedures for {category} issues.</p>
        </div>
        <div class="footer">
            <p>This email was automatically forwarded by the AI-powered Smart Email Management System.<br>
            © 2025 Team Wanderers. All rights reserved.</p>
        </div>
    </body>
    </html>
    """
    return html

def forward_email(subject, body, to_email, sender):
    try:
        category = classify_email(body)
        sentiment = analyze_sentiment(body)
        language = detect_language(body)
        summary = summarize_email(body) if len(body.split()) > 100 else ""
        customer_id = extract_customer_id(body)
        priority = calculate_priority(subject, body, sentiment)
        
        msg = MIMEMultipart('alternative')
        msg["Subject"] = f"[{category}][P{priority}] {subject}"
        msg["From"] = EMAIL_USER
        msg["To"] = to_email
        msg["Reply-To"] = sender
        
        text_content = f"Subject: {subject}\n\nFrom: {sender}\n\nCategory: {category}\n\nPriority: {priority}/5\n\nSentiment: {sentiment}\n\nLanguage: {language}\n\nMessage:\n{body}\n\nThis email was automatically forwarded by the Smart Email Management System."
        html_content = create_email_template(subject, body, category, sender, sentiment, priority, summary, language, customer_id)
        
        part1 = MIMEText(text_content, 'plain')
        part2 = MIMEText(html_content, 'html')
        msg.attach(part1)
        msg.attach(part2)

        print(f"Attempting to forward email to {to_email}...")
        
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
            print(f"Email successfully forwarded to {to_email}")
            
            auto_response = generate_auto_response(body, category, sentiment)
            if auto_response:
                store_auto_response(sender, subject, auto_response, category)
                if category in ["General Inquiry", "Technical"] or priority <= 3:
                    send_auto_response(sender, auto_response, subject)
            
            return True
    except Exception as e:
        print(f"Email forwarding error: {str(e)}")
        return False

def store_email(category, sender, subject, body, forwarded_to=None):
    try:
        sentiment = analyze_sentiment(body)
        language = detect_language(body)
        summary = summarize_email(body) if len(body.split()) > 100 else ""
        customer_id = extract_customer_id(body)
        priority = calculate_priority(subject, body, sentiment)
        
        email_doc = {
            "category": category,
            "sender": sender,
            "subject": subject,
            "body": body,
            "summary": summary,
            "forwarded_to": forwarded_to,
            "sentiment": sentiment,
            "priority": priority,
            "language": language,
            "customer_id": customer_id,
            "response_time": None,
            "status": "pending",
            "timestamp": datetime.now()
        }
        emails_collection.insert_one(email_doc)
        return True
    except Exception as e:
        print("Database Error:", e)
        return False

def store_auto_response(recipient, subject, response_text, category, is_auto=True):
    """Store auto-generated responses in the database."""
    try:
        response_doc = {
            "recipient": recipient,
            "subject": subject,
            "response_text": response_text,
            "category": category,
            "is_auto": is_auto,
            "timestamp": datetime.now()
        }
        responses_collection.insert_one(response_doc)
        return True
    except Exception as e:
        print("Response Storage Error:", e)
        return False

def get_emails_by_category():
    try:
        email_data = {
            "Technical": [],
            "Billing": [],
            "Complaint": [],
            "General Inquiry": [],
            "Unclassified": []
        }
        
        emails = emails_collection.find().sort("timestamp", -1)
        
        for email_doc in emails:
            category = email_doc['category']
            email_data.setdefault(category, []).append({
                "_id": str(email_doc['_id']),  # Convert ObjectId to string to make it accessible in templates
                "from": email_doc['sender'],
                "subject": email_doc['subject'],
                "body": email_doc['body'],
                "summary": email_doc.get('summary', ''),
                "sentiment": email_doc.get('sentiment', 'Neutral'),
                "priority": email_doc.get('priority', 3),
                "language": email_doc.get('language', 'en'),
                "customer_id": email_doc.get('customer_id', ''),
                "forwarded_to": email_doc.get('forwarded_to', 'Not recorded'),
                "timestamp": email_doc['timestamp'],
                "status": email_doc.get('status', 'pending')
            })
            
        return email_data
    except Exception as e:
        print("Database Read Error:", e)
        return {"Technical": [], "Billing": [], "Complaint": [], "General Inquiry": [], "Unclassified": []}

def generate_weekly_report():
    """Generate a weekly analytics report."""
    try:
        one_week_ago = datetime.now() - timedelta(days=7)
        emails = emails_collection.find({"timestamp": {"$gte": one_week_ago}})
        
        total_emails = 0
        categories = {}
        sentiments = {"Positive": 0, "Neutral": 0, "Negative": 0, "Very Negative": 0}
        priorities = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        languages = {}
        response_times = []
        
        for email in emails:
            total_emails += 1
            
            category = email.get('category', 'Unclassified')
            categories[category] = categories.get(category, 0) + 1
            
            sentiment = email.get('sentiment', 'Neutral')
            sentiments[sentiment] = sentiments.get(sentiment, 0) + 1
            
            priority = email.get('priority', 3)
            priorities[priority] = priorities.get(priority, 0) + 1
            
            language = email.get('language', 'en')
            languages[language] = languages.get(language, 0) + 1
            
            if email.get('status') == 'resolved' and email.get('response_time'):
                response_duration = email.get('response_time') - email.get('timestamp')
                response_times.append(response_duration.total_seconds() / 3600)
        
        avg_response_time = sum(response_times) / len(response_times) if response_times else 0
                
        report = {
            "period": {
                "start": one_week_ago.strftime("%Y-%m-%d"),
                "end": datetime.now().strftime("%Y-%m-%d")
            },
            "total_emails": total_emails,
            "categories": categories,
            "sentiments": sentiments,
            "priorities": priorities,
            "languages": languages,
            "response_metrics": {
                "avg_response_time": avg_response_time
            },
            "by_category": {}
        }
        
        # Get category stats
        for category in list(DEPARTMENTS.keys()) + ["Unclassified"]:
            completed = emails_collection.count_documents({
                "category": category,
                "status": "resolved",
                "timestamp": {"$gte": one_week_ago}
            })
            
            pending = emails_collection.count_documents({
                "category": category,
                "status": {"$in": ["pending", "in-progress"]},
                "timestamp": {"$gte": one_week_ago}
            })
            
            report["by_category"][category] = {
                "total": completed + pending,
                "completed": completed,
                "pending": pending
            }
            
        return report
    
    except Exception as e:
        print("Weekly Report Generation Error:", e)
        return {}

def get_response_statistics():
    """Get statistics about response times and volumes."""
    try:
        now = datetime.now()
        seven_days_ago = now - timedelta(days=7)
        thirty_days_ago = now - timedelta(days=30)
        
        # Daily response counts for the last 7 days
        daily_counts = []
        for i in range(7):
            day_start = now - timedelta(days=i+1)
            day_end = now - timedelta(days=i)
            count = emails_collection.count_documents({
                "timestamp": {"$gte": day_start, "$lt": day_end}
            })
            daily_counts.append({
                "date": day_start.strftime("%Y-%m-%d"),
                "count": count
            })
        
        # Category distribution
        categories = {}
        for cat in list(DEPARTMENTS.keys()) + ["Unclassified"]:
            categories[cat] = emails_collection.count_documents({
                "category": cat,
                "timestamp": {"$gte": thirty_days_ago}
            })
        
        # Priority distribution
        priorities = {}
        for p in range(1, 6):
            priorities[p] = emails_collection.count_documents({
                "priority": p,
                "timestamp": {"$gte": thirty_days_ago}
            })
        
        # Average response times by category
        avg_times = {}
        for cat in list(DEPARTMENTS.keys()) + ["Unclassified"]:
            emails = emails_collection.find({
                "category": cat,
                "status": "resolved",
                "timestamp": {"$gte": thirty_days_ago},
                "response_time": {"$ne": None}
            })
            
            times = []
            for email in emails:
                response_duration = email.get('response_time') - email.get('timestamp')
                times.append(response_duration.total_seconds() / 3600)
            
            avg_times[cat] = sum(times) / len(times) if times else 0
            
        # Count total responses
        total_responses = emails_collection.count_documents({
            "status": "resolved",
            "timestamp": {"$gte": seven_days_ago}
        })
        
        return {
            "daily_counts": daily_counts,
            "categories": categories,
            "priorities": priorities,
            "avg_response_times": avg_times,
            "total_responses": total_responses
        }
    except Exception as e:
        print("Response Statistics Error:", e)
        return {}

def fetch_and_process_emails():
    global polling_active
    while True:
        if polling_active:  # Only fetch emails if polling is active
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(EMAIL_USER, EMAIL_PASS)
                mail.select("inbox")

                _, messages = mail.search(None, "UNSEEN")
                email_ids = messages[0].split()
                
                print(f"Found {len(email_ids)} unread email(s)")
                
                processed_count = 0

                for eid in email_ids:
                    _, msg_data = mail.fetch(eid, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])

                    subject, encoding = decode_header(msg["Subject"])[0]
                    if isinstance(subject, bytes):
                        subject = subject.decode(encoding or "utf-8")
                    sender = msg.get("From")

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    category = classify_email(body)
                    
                    to_email = DEPARTMENTS.get(category)
                    if not to_email:
                        print(f"Warning: No email configured for category '{category}'. Using general email.")
                        to_email = os.getenv("GENERAL_EMAIL", EMAIL_USER)
                    
                    print(f"Attempting to forward as '{category}' to {to_email}")
                    forward_result = forward_email(subject, body, to_email, sender)
                    
                    if forward_result:
                        print(f"Successfully forwarded '{subject}' to {to_email} (Category: {category})")
                    else:
                        print(f"Failed to forward '{subject}' to {to_email} (Category: {category})")

                    store_email(category, sender, subject, body, to_email)

                    mail.store(eid, '+FLAGS', '\\Seen')
                    
                    processed_count += 1
                    
                print(f"Successfully processed {processed_count}/{len(email_ids)} unread email(s)")
                
                mail.logout()
            except Exception as e:
                print("Polling Error:", e)
        else:
            print("Email polling is paused")

        time.sleep(30)

threading.Thread(target=fetch_and_process_emails, daemon=True).start()

def get_email_details(email_id):
    """Get detailed information about a specific email."""
    try:
        from bson.objectid import ObjectId
        try:
            # Try to convert the email_id to ObjectId
            email = emails_collection.find_one({"_id": ObjectId(email_id)})
        except Exception as e:
            print(f"Invalid ObjectId format: {str(e)}")
            # If conversion fails, try to find by string ID (just in case)
            email = emails_collection.find_one({"_id": email_id})
        
        if not email:
            print(f"Email not found with ID: {email_id}")
            return None
            
        # Get any associated responses
        responses = responses_collection.find({
            "recipient": email.get('sender'),
            "subject": {"$regex": re.escape(email.get('subject', '')), "$options": "i"}
        }).sort("timestamp", 1)
        
        # Format the email with responses
        email_details = {
            "id": str(email.get('_id')),
            "category": email.get('category', 'Unclassified'),
            "sender": email.get('sender', ''),
            "subject": email.get('subject', ''),
            "body": email.get('body', ''),
            "summary": email.get('summary', ''),
            "forwarded_to": email.get('forwarded_to', ''),
            "sentiment": email.get('sentiment', 'Neutral'),
            "priority": email.get('priority', 3),
            "language": email.get('language', 'en'),
            "customer_id": email.get('customer_id', ''),
            "status": email.get('status', 'pending'),
            "timestamp": email.get('timestamp').strftime("%Y-%m-%d %H:%M:%S") if email.get('timestamp') else '',
            "response_time": email.get('response_time').strftime("%Y-%m-%d %H:%M:%S") if email.get('response_time') else '',
            "responses": []
        }
        
        for response in responses:
            email_details["responses"].append({
                "response_text": response.get('response_text', ''),
                "is_auto": response.get('is_auto', True),
                "timestamp": response.get('timestamp').strftime("%Y-%m-%d %H:%M:%S") if response.get('timestamp') else ''
            })
            
        return email_details
    except Exception as e:
        print(f"Email Details Error: {str(e)}")
        return None

@app.route('/')
def dashboard():
    email_log = get_emails_by_category()
    return render_template('dashboard.html', email_log=email_log, polling_active=polling_active)

@app.route('/analytics')
def analytics():
    weekly_report = generate_weekly_report()
    response_stats = get_response_statistics()
    
    return render_template('analytics.html', 
                          report=weekly_report, 
                          response_stats=response_stats)

@app.route('/view-email/<email_id>')
def view_email(email_id):
    try:
        email_details = get_email_details(email_id)
        if not email_details:
            return "Email not found", 404
        return render_template('email_details.html', email=email_details)
    except Exception as e:
        print(f"Error viewing email: {str(e)}")
        return f"Error loading email details: {str(e)}", 500

@app.route('/api/weekly-report')
def api_weekly_report():
    return jsonify(generate_weekly_report())

@app.route('/api/response-stats')
def api_response_stats():
    return jsonify(get_response_statistics())

@app.route('/api/email-details/<email_id>')
def api_email_details(email_id):
    details = get_email_details(email_id)
    if not details:
        return jsonify({"error": "Email not found"}), 404
    return jsonify(details)

@app.route('/manual-response', methods=['POST'])
def manual_response():
    try:
        data = request.json
        email_id = data.get('email_id')
        response_text = data.get('response')
        send_copy = data.get('send_copy', False)
        
        from bson.objectid import ObjectId
        email = emails_collection.find_one({"_id": ObjectId(email_id)})
        
        if not email:
            return jsonify({"success": False, "error": "Email not found"})
        
        # Update email status in database
        emails_collection.update_one(
            {"_id": ObjectId(email_id)},
            {
                "$set": {
                    "status": "resolved",
                    "response_time": datetime.now()
                }
            }
        )
        
        # Store response in database
        store_auto_response(
            email.get('sender'),
            f"RE: {email.get('subject')}",
            response_text,
            email.get('category'),
            is_auto=False
        )
        
        # Send response email if requested
        if send_copy and email.get('sender'):
            try:
                msg = MIMEMultipart('alternative')
                msg["Subject"] = f"RE: {email.get('subject')}"
                msg["From"] = EMAIL_USER
                msg["To"] = email.get('sender')
                
                html_content = f"""
                <!DOCTYPE html>
                <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #ddd; border-radius: 5px;">
                        <div style="border-bottom: 2px solid #4285F4; padding-bottom: 10px; margin-bottom: 20px;">
                            <h2 style="color: #4285F4; margin: 0;">Response to Your Inquiry</h2>
                        </div>
                        
                        <p>Dear Customer,</p>
                        
                        <p>{response_text}</p>
                        
                        <p style="margin-top: 30px;">Best regards,<br>
                        Customer Support Team<br>
                        Team Wanderers</p>
                    </div>
                </body>
                </html>
                """
                
                text_content = f"""
                Response to Your Inquiry
                
                Dear Customer,
                
                {response_text}
                
                Best regards,
                Customer Support Team
                Team Wanderers
                """
                
                part1 = MIMEText(text_content, 'plain')
                part2 = MIMEText(html_content, 'html')
                msg.attach(part1)
                msg.attach(part2)
                
                with smtplib.SMTP("smtp.gmail.com", 587) as server:
                    server.starttls()
                    server.login(EMAIL_USER, EMAIL_PASS)
                    server.send_message(msg)
            except Exception as e:
                print(f"Error sending response email: {str(e)}")
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/change-status', methods=['POST'])
def change_status():
    try:
        data = request.json
        email_id = data.get('email_id')
        new_status = data.get('status')
        
        valid_statuses = ['pending', 'in-progress', 'resolved', 'closed']
        if new_status not in valid_statuses:
            return jsonify({"success": False, "error": "Invalid status value"})
        
        from bson.objectid import ObjectId
        
        update_data = {
            "status": new_status
        }
        
        if new_status == 'resolved':
            update_data["response_time"] = datetime.now()
        
        emails_collection.update_one(
            {"_id": ObjectId(email_id)},
            {"$set": update_data}
        )
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/reassign-category', methods=['POST'])
def reassign_category():
    try:
        data = request.json
        email_id = data.get('email_id')
        new_category = data.get('category')
        
        valid_categories = list(DEPARTMENTS.keys()) + ["Unclassified"]
        if new_category not in valid_categories:
            return jsonify({"success": False, "error": "Invalid category"})
        
        from bson.objectid import ObjectId
        
        # Get the email first
        email = emails_collection.find_one({"_id": ObjectId(email_id)})
        if not email:
            return jsonify({"success": False, "error": "Email not found"})
        
        # Update the category
        emails_collection.update_one(
            {"_id": ObjectId(email_id)},
            {"$set": {
                "category": new_category
            }}
        )
        
        # Forward to new department if needed
        to_email = DEPARTMENTS.get(new_category)
        if to_email and to_email != email.get('forwarded_to'):
            # Forward to new department
            try:
                forward_email(
                    email.get('subject', ''), 
                    email.get('body', ''), 
                    to_email, 
                    email.get('sender', '')
                )
                
                # Update forwarded_to field
                emails_collection.update_one(
                    {"_id": ObjectId(email_id)},
                    {"$set": {"forwarded_to": to_email}}
                )
            except Exception as e:
                print(f"Error forwarding to new department: {str(e)}")
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/generate-response', methods=['POST'])
def generate_ai_response():
    try:
        data = request.json
        email_id = data.get('email_id')
        
        from bson.objectid import ObjectId
        email = emails_collection.find_one({"_id": ObjectId(email_id)})
        
        if not email:
            return jsonify({"success": False, "error": "Email not found"})
            
        body = email.get('body', '')
        category = email.get('category', 'General Inquiry')
        sentiment = email.get('sentiment', 'Neutral')
        
        generated_response = generate_auto_response(body, category, sentiment)
        
        if not generated_response:
            return jsonify({"success": False, "error": "Failed to generate response"})
            
        return jsonify({
            "success": True, 
            "response": generated_response
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/toggle-polling', methods=['POST'])
def toggle_polling():
    try:
        global polling_active
        data = request.json
        polling_active = data.get('active', False)
        print(f"Email polling set to: {polling_active}")
        return jsonify({"success": True, "active": polling_active})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.template_filter('format_datetime')
def format_datetime(value, format='%Y-%m-%d %H:%M'):
    """Format a datetime object to string."""
    if isinstance(value, datetime):
        return value.strftime(format)
    return value

@app.template_filter('time_ago')
def time_ago(dt):
    """Format a datetime as time ago string."""
    if not isinstance(dt, datetime):
        return ""
        
    now = datetime.now()
    diff = now - dt
    
    seconds = diff.total_seconds()
    if seconds < 60:
        return "Just now"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    elif seconds < 604800:
        days = int(seconds // 86400)
        return f"{days} day{'s' if days > 1 else ''} ago"
    else:
        return dt.strftime("%Y-%m-%d")

@app.template_filter('priority_color')
def priority_color(priority):
    """Get color for priority level."""
    colors = {
        1: "#34a853",  # Green (low)
        2: "#8abb6f",  # Light green
        3: "#fbbc05",  # Yellow (medium)
        4: "#ff914d",  # Orange
        5: "#ea4335"   # Red (high)
    }
    return colors.get(priority, "#fbbc05")

@app.template_filter('sentiment_color')
def sentiment_color(sentiment):
    """Get color for sentiment level."""
    colors = {
        "Positive": "#34a853",     # Green
        "Neutral": "#fbbc05",      # Yellow
        "Negative": "#ff914d",     # Orange
        "Very Negative": "#ea4335" # Red
    }
    return colors.get(sentiment, "#fbbc05")

if __name__ == '__main__':
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    os.makedirs(templates_dir, exist_ok=True)
    
    app.run(debug=True,port=5000)
