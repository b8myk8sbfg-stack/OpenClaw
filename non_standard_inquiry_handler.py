import os
import csv
import json
import datetime
import requests
from dotenv import load_dotenv
from O365 import Account

load_dotenv()

VERSION = "v1.01-NON-STANDARD-INQUIRY-WEB-SUGGESTIONS"

NON_STANDARD_CSV = "/Users/evon/OpenClaw/non_standard_inquiries.csv"
TECHNICAL_EMAIL = "support@robomatics.sg"

# Add this to .env later:
# SERPAPI_API_KEY=your_key_here
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "").strip()

SEARCH_ENABLED = True
MAX_LOCAL_RESULTS = 5
MAX_OVERSEAS_RESULTS = 5


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def ensure_csv():
    fields = [
        "created_at",
        "customer_name",
        "customer_contact",
        "channel",
        "brand",
        "part_no",
        "qty",
        "source_message",
        "search_suggestions_json",
        "status"
    ]

    if not os.path.exists(NON_STANDARD_CSV):
        with open(NON_STANDARD_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

    return fields


def save_non_standard_item(customer_name, customer_contact, channel, item, source_message, suggestions):
    fields = ensure_csv()

    with open(NON_STANDARD_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({
            "created_at": now_iso(),
            "customer_name": customer_name or "",
            "customer_contact": customer_contact or "",
            "channel": channel or "",
            "brand": item.get("brand", "UNKNOWN"),
            "part_no": item.get("part_no") or item.get("pid") or item.get("desc", ""),
            "qty": item.get("qty", ""),
            "source_message": source_message or "",
            "search_suggestions_json": json.dumps(suggestions, ensure_ascii=False),
            "status": "PENDING_TECHNICAL_REVIEW"
        })


def serpapi_search(query, location="Malaysia", gl="my", num=5):
    if not SERPAPI_API_KEY:
        print("⚠️ [NON-STANDARD] SERPAPI_API_KEY not set. Search skipped.")
        return []

    params = {
        "engine": "google",
        "q": query,
        "api_key": SERPAPI_API_KEY,
        "location": location,
        "gl": gl,
        "num": num
    }

    try:
        response = requests.get(
            "https://serpapi.com/search.json",
            params=params,
            timeout=20
        )
        response.raise_for_status()
        data = response.json()

        results = []

        for result in data.get("organic_results", [])[:num]:
            results.append({
                "title": result.get("title", ""),
                "url": result.get("link", ""),
                "snippet": result.get("snippet", "")
            })

        return results

    except Exception as e:
        print(f"❌ [NON-STANDARD] SerpAPI search error: {e}")
        return []


def search_supplier_suggestions(item):
    if not SEARCH_ENABLED:
        return []

    part_no = item.get("part_no") or item.get("pid") or item.get("desc", "")
    brand = item.get("brand", "")
    desc = item.get("desc", "")

    search_text = " ".join(x for x in [brand, part_no, desc] if x).strip()

    suggestions = []

    local_query = f'{search_text} supplier Malaysia distributor'
    overseas_query = f'{search_text} supplier distributor buy online'

    print(f"🔎 [NON-STANDARD] Local supplier search: {local_query}")
    local_results = serpapi_search(
        query=local_query,
        location="Malaysia",
        gl="my",
        num=MAX_LOCAL_RESULTS
    )

    for r in local_results:
        suggestions.append({
            "priority": "LOCAL_MALAYSIA",
            "query": local_query,
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", "")
        })

    print(f"🌍 [NON-STANDARD] Overseas supplier search: {overseas_query}")
    overseas_results = serpapi_search(
        query=overseas_query,
        location="Singapore",
        gl="sg",
        num=MAX_OVERSEAS_RESULTS
    )

    for r in overseas_results:
        suggestions.append({
            "priority": "OVERSEAS",
            "query": overseas_query,
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", "")
        })

    return suggestions


def get_mailbox():
    acc = Account(
        (os.getenv("MICROSOFT_CLIENT_ID"), os.getenv("MICROSOFT_CLIENT_SECRET")),
        auth_flow_type="credentials",
        tenant_id=os.getenv("MICROSOFT_TENANT_ID")
    )

    if not acc.authenticate():
        print("❌ [NON-STANDARD] Microsoft authentication failed.")
        return None

    return acc.mailbox(resource="evon@robomatics.sg")


def build_technical_email_body(customer_name, customer_contact, channel, items, source_message, all_suggestions):
    rows = ""

    for idx, item in enumerate(items, start=1):
        part_no = item.get("part_no") or item.get("pid") or item.get("desc", "")
        brand = item.get("brand", "UNKNOWN")
        qty = item.get("qty", "")

        suggestions = all_suggestions.get(part_no, [])

        if suggestions:
            suggestion_text = ""
            for s in suggestions:
                suggestion_text += (
                    f"<b>{s.get('priority', '')}</b><br>"
                    f"{s.get('title', '')}<br>"
                    f"<a href='{s.get('url', '')}'>{s.get('url', '')}</a><br>"
                    f"{s.get('snippet', '')}<br><br>"
                )
        else:
            suggestion_text = (
                "No search results available. "
                "Check SERPAPI_API_KEY or search manually."
            )

        rows += f"""
        <tr>
            <td>{idx}</td>
            <td>{brand}</td>
            <td>{part_no}</td>
            <td>{qty}</td>
            <td>{suggestion_text}</td>
        </tr>
        """

    return f"""
    Hi Technical Team,<br><br>

    The bot detected non-standard item(s) that are not confirmed in the warehouse/OBM product master.
    Please help source or verify manually.<br><br>

    <b>Customer:</b> {customer_name}<br>
    <b>Contact:</b> {customer_contact}<br>
    <b>Channel:</b> {channel}<br>
    <b>Created At:</b> {now_iso()}<br><br>

    <table border="1" cellpadding="5" style="border-collapse: collapse;">
        <tr>
            <th>No.</th>
            <th>Brand</th>
            <th>Part No / Description</th>
            <th>Qty</th>
            <th>Supplier Search Suggestions</th>
        </tr>
        {rows}
    </table>

    <br><br>
    <b>Original Customer Message:</b><br>
    <pre>{source_message}</pre>

    <br>
    Regards,<br>
    Evon
    """


def send_to_technical(customer_name, customer_contact, channel, items, source_message, all_suggestions):
    mailbox = get_mailbox()

    if not mailbox:
        return False

    msg = mailbox.new_message()
    msg.to.add(TECHNICAL_EMAIL)
    msg.subject = f"Non-Standard Sourcing Request - {customer_name or customer_contact}"
    msg.body = build_technical_email_body(
        customer_name,
        customer_contact,
        channel,
        items,
        source_message,
        all_suggestions
    )
    msg.body_type = "html"
    msg.send()

    print("📧 [NON-STANDARD] Technical email sent.")
    print(f"   To: {TECHNICAL_EMAIL}")
    print(f"   Items: {len(items)}")

    return True


def handle_non_standard_items(customer_name, customer_contact, channel, items, source_message):
    if not items:
        return {
            "handled": False,
            "reason": "No non-standard items supplied"
        }

    print("")
    print("=" * 90)
    print(f"🧩 [NON-STANDARD] Handler Active - {VERSION}")
    print(f"   Customer: {customer_name}")
    print(f"   Contact: {customer_contact}")
    print(f"   Channel: {channel}")
    print(f"   Items: {len(items)}")
    print("=" * 90)

    all_suggestions = {}

    for item in items:
        part_no = item.get("part_no") or item.get("pid") or item.get("desc", "")
        suggestions = search_supplier_suggestions(item)
        all_suggestions[part_no] = suggestions

        save_non_standard_item(
            customer_name=customer_name,
            customer_contact=customer_contact,
            channel=channel,
            item=item,
            source_message=source_message,
            suggestions=suggestions
        )

        print(f"   📝 Logged non-standard item: {part_no} | Qty: {item.get('qty')}")

    email_sent = send_to_technical(
        customer_name=customer_name,
        customer_contact=customer_contact,
        channel=channel,
        items=items,
        source_message=source_message,
        all_suggestions=all_suggestions
    )

    return {
        "handled": True,
        "technical_email_sent": email_sent,
        "items_count": len(items)
    }