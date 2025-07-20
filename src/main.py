import os
import base64
from flask import Flask, request, redirect, url_for, render_template_string
from redis import Redis
from dotenv import load_dotenv
import openai
import uuid

load_dotenv()

app = Flask(__name__)
openai.api_key = os.getenv("OPENAI_API_KEY")
redis = Redis.from_url(os.getenv("REDIS_URL"))

# HTML Templates (using Flask's render_template_string for KISS)
INDEX_HTML = """
<h2>AI Receipt Uploader</h2>
<form action="/upload" method="post" enctype="multipart/form-data">
  <input type="file" name="receipt" accept="image/*,application/pdf" required>
  <button type="submit">Upload & Extract</button>
</form>
<br>
<a href="/receipts">View All Receipts</a>
"""

TABLE_HTML = """
<h2>Receipts Table</h2>
<table border="1" cellpadding="4">
<tr>
  <th>ID</th>
  <th>Vendor</th>
  <th>Date</th>
  <th>Amount</th>
  <th>Currency</th>
  <th>Raw Text</th>
  <th>Receipt</th>
</tr>
{% for r in records %}
<tr>
  <td>{{r['id']}}</td>
  <td>{{r['data'].get('vendor','')}}</td>
  <td>{{r['data'].get('date','')}}</td>
  <td>{{r['data'].get('amount','')}}</td>
  <td>{{r['data'].get('currency','')}}</td>
  <td>{{r['data'].get('text_excerpt','')}}</td>
  <td>
    {% if r['filename'] %}
      <a href="/receipt/{{r['id']}}">View</a>
    {% endif %}
  </td>
</tr>
{% endfor %}
</table>
<br><a href="/">Go Back</a>
"""

# ---- Helper functions ----

def save_to_redis(receipt_id, data, file_bytes=None, filename=None):
    entry = {
        "id": receipt_id,
        "data": data,
        "filename": filename or "",
    }
    redis.set(f"receipt:{receipt_id}:data", str(entry))
    if file_bytes:
        # store file as base64 in redis for simplicity
        redis.set(f"receipt:{receipt_id}:file", base64.b64encode(file_bytes).decode())

def get_all_receipts():
    keys = redis.keys("receipt:*:data")
    receipts = []
    for k in keys:
        raw = redis.get(k)
        try:
            entry = eval(raw)  # KISS for demo - in prod use safe json!
            receipts.append(entry)
        except Exception:
            pass
    # For each, add a text excerpt for table
    for r in receipts:
        text = r["data"].get("raw_text", "")
        r["data"]["text_excerpt"] = text[:50] + "..." if len(text) > 50 else text
    return receipts

# ---- OpenAI Vision Receipt Extraction ----

def extract_receipt_fields(file_bytes):
    # Use OpenAI GPT-4 with Vision to extract the core fields
    # Simplest: Send as base64 image, get a short JSON in reply
    encoded_file = base64.b64encode(file_bytes).decode()

    SYSTEM_PROMPT = (
        "You are a finance assistant. Extract ONLY these fields from "
        "the receipt image/PDF: vendor, date, amount, currency. "
        "Return only a JSON object. If absent, null the field. "
        "Also return a 'raw_text' field with all text you see."
    )

    # See https://platform.openai.com/docs/guides/vision
    response = openai.chat.completions.create(
        model="gpt-4-vision-preview",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    # A file dict for vision
                    {"type": "image", "image": encoded_file}
                ],
            },
        ],
        max_tokens=400
    )

    # Parse JSON from AI reply
    import json
    import re
    ai_text = response.choices[0].message.content
    # Try to extract JSON from string (sometimes returns as code block)
    match = re.search(r"\{(?:[^{}]|(?R))*\}", ai_text)
    if match:
        result = json.loads(match.group())
    else:
        result = {"raw_text": ai_text}
    return result

# ---- Routes ----

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

@app.route("/upload", methods=["POST"])
def upload():
    file = request.files["receipt"]
    if file:
        file_bytes = file.read()
        filename = file.filename
        # Extract fields with AI
        data = extract_receipt_fields(file_bytes)
        receipt_id = str(uuid.uuid4())
        save_to_redis(receipt_id, data, file_bytes, filename)
        return redirect(url_for("receipts"))
    return "No file!", 400

@app.route("/receipts", methods=["GET"])
def receipts():
    records = get_all_receipts()
    return render_template_string(TABLE_HTML, records=records)

@app.route("/receipt/<receipt_id>")
def receipt_file(receipt_id):
    b64file = redis.get(f"receipt:{receipt_id}:file")
    entry = redis.get(f"receipt:{receipt_id}:data")
    if not (b64file and entry):
        return "Not found", 404
    entry = eval(entry)
    filename = entry.get("filename", "receipt")
    file_bytes = base64.b64decode(b64file)
    # Simple content-type detection
    if filename.lower().endswith(".pdf"):
        mimetype = "application/pdf"
    else:
        mimetype = "image/jpeg"
    return (file_bytes, 200, {
        "Content-Type": mimetype,
        "Content-Disposition": f'inline; filename="{filename}"'
    })

if __name__ == "__main__":
    app.run(debug=True)
