from __future__ import annotations

import os
import re
import csv
import json
import datetime
import html
from typing import Any
import requests
from dotenv import load_dotenv
from O365 import Account

load_dotenv()

VERSION = "v1.04-TECHNICAL-EMAIL-VERIFIED-LINKS"

NON_STANDARD_CSV = "/Users/evon/OpenClaw/non_standard_inquiries.csv"
TECHNICAL_EMAIL = "support@robomatics.sg"

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")
COPILOT_API_KEY = os.getenv("COPILOT_API_KEY", "local-copilot-proxy")

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()

SEARCH_ENABLED = os.getenv("OPENCLAW_SUPPLIER_SEARCH", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
COPILOT_SUPPLIER_SEARCH = os.getenv("OPENCLAW_COPILOT_SUPPLIER_SEARCH", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
SERPAPI_SUPPLIER_FALLBACK = os.getenv("OPENCLAW_SERPAPI_SUPPLIER_FALLBACK", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
TAVILY_SUPPLIER_FALLBACK = os.getenv("OPENCLAW_TAVILY_SUPPLIER_FALLBACK", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

MAX_LOCAL_RESULTS = 5
MAX_OVERSEAS_RESULTS = 5
MAX_TAVILY_RESULTS = 3
COPILOT_TIMEOUT_SECS = float(os.getenv("OPENCLAW_COPILOT_SUPPLIER_TIMEOUT", "120"))

LINK_VERIFY_ENABLED = os.getenv("OPENCLAW_VERIFY_SUPPLIER_LINKS", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
LINK_VERIFY_TIMEOUT = float(os.getenv("OPENCLAW_SUPPLIER_LINK_TIMEOUT", "8"))
MAX_TECHNICAL_SUPPLIERS_PER_ITEM = int(os.getenv("OPENCLAW_TECH_EMAIL_MAX_SUPPLIERS", "8"))

_PRIORITY_LABELS = {
    "COPILOT_LOCAL": "Malaysia",
    "COPILOT_OVERSEAS": "International",
    "LOCAL_MALAYSIA": "Malaysia",
    "OVERSEAS": "International",
    "TAVILY": "Web search",
}

_link_verify_cache: dict[str, dict] = {}

_COPILOT_SYSTEM_PROMPT = (
    "You are a procurement research assistant with live web search. "
    "Search the web for real supplier companies. "
    "Return ONLY valid JSON — no markdown, no explanation. "
    'Schema: {"suppliers": [{"brand": "...", "website": "https://..."}]}. '
    "Include 4-8 real suppliers with working websites. "
    "Prefer manufacturers/distributors over generic marketplaces unless no better match."
)

_copilot_client = None


_UNKNOWN_TOKENS = frozenset({"", "UNKNOWN", "N/A", "NA", "TBC", "NIL", "NONE"})


def _is_unknown_part_token(part_no: str) -> bool:
    return str(part_no or "").strip().upper() in _UNKNOWN_TOKENS


def _item_lookup_key(item: dict) -> str:
    part_no = str(item.get("part_no") or item.get("pid") or "").strip()
    desc = str(item.get("desc") or "").strip()
    if _is_unknown_part_token(part_no):
        return desc or part_no
    return part_no or desc


def _dedupe_suggestions(suggestions: list[dict]) -> list[dict]:
    seen_urls: set[str] = set()
    merged: list[dict] = []
    for row in suggestions:
        url = str(row.get("url") or row.get("website") or "").strip().lower()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        merged.append(row)
    return merged



def _item_search_spec(item: dict) -> str:
    """Build a natural-language spec string for supplier search queries."""
    key = _item_lookup_key(item)
    brand = str(item.get("brand") or "").strip()
    if brand and brand.upper() not in ("UNKNOWN", "N/A"):
        return f"{brand} {key}".strip()
    return key.strip() or "industrial component"


def _extract_json_from_copilot(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines).strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _get_copilot_client():
    global _copilot_client
    if _copilot_client is not None:
        return _copilot_client
    try:
        from openai import OpenAI
    except ImportError:
        print("⚠️ [NON-STANDARD] openai package not installed — Copilot search skipped.")
        return None
    _copilot_client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=COPILOT_API_KEY,
        timeout=COPILOT_TIMEOUT_SECS,
        max_retries=0,
    )
    return _copilot_client


def _copilot_query_suppliers(query: str, priority: str) -> list[dict]:
    client = _get_copilot_client()
    if not client:
        return []

    print(f"🤖 [NON-STANDARD] Copilot supplier search ({priority}): {query}")
    try:
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {"role": "system", "content": _COPILOT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({"query": query})},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"❌ [NON-STANDARD] Copilot search error ({priority}): {exc}")
        return []

    parsed = _extract_json_from_copilot(text)
    if not parsed:
        print(f"⚠️ [NON-STANDARD] Copilot returned malformed JSON ({priority})")
        return []

    suppliers = parsed.get("suppliers")
    if not isinstance(suppliers, list) or not suppliers:
        print(f"⚠️ [NON-STANDARD] Copilot returned no suppliers ({priority})")
        return []

    rows = []
    for row in suppliers:
        if not isinstance(row, dict):
            continue
        brand = str(row.get("brand") or "").strip()
        website = str(row.get("website") or row.get("url") or "").strip()
        if not brand and not website:
            continue
        rows.append({
            "priority": priority,
            "query": query,
            "title": brand or website,
            "url": website,
            "snippet": "",
            "source": "copilot",
        })
    return rows


def copilot_search_suppliers(item: dict) -> list[dict]:
    """
    Primary supplier search via Copilot web search.

    Per item: local Malaysia + international queries using the item spec/length.
    """
    if not COPILOT_SUPPLIER_SEARCH:
        return []

    spec = _item_search_spec(item)
    if not spec:
        return []

    local_query = f"supplier brands and websites in Malaysia for {spec}"
    overseas_query = f"international supplier brands and websites for {spec}"

    suggestions: list[dict] = []
    suggestions.extend(_copilot_query_suppliers(local_query, "COPILOT_LOCAL"))
    suggestions.extend(_copilot_query_suppliers(overseas_query, "COPILOT_OVERSEAS"))
    return _dedupe_suggestions(suggestions)


def _fallback_serpapi_tavily_suggestions(item: dict) -> list[dict]:
    """SerpAPI + Tavily fallback when Copilot returns empty or malformed results."""
    part_no = item.get("part_no") or item.get("pid") or ""
    desc = item.get("desc", "")
    search_text = " ".join(
        x for x in [_item_search_spec(item)]
        if x and str(x).upper() not in ("UNKNOWN", "N/A")
    ).strip() or str(desc or part_no).strip()

    suggestions: list[dict] = []

    if SERPAPI_SUPPLIER_FALLBACK and SERPAPI_API_KEY:
        local_query = f"{search_text} supplier Malaysia distributor"
        overseas_query = f"{search_text} supplier distributor buy online"

        print(f"🔎 [NON-STANDARD] Fallback local search (SerpAPI): {local_query}")
        for r in serpapi_search(query=local_query, location="Malaysia", gl="my", num=MAX_LOCAL_RESULTS):
            suggestions.append({
                "priority": "LOCAL_MALAYSIA",
                "query": local_query,
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
                "source": "serpapi",
            })

        print(f"🌍 [NON-STANDARD] Fallback overseas search (SerpAPI): {overseas_query}")
        for r in serpapi_search(query=overseas_query, location="Singapore", gl="sg", num=MAX_OVERSEAS_RESULTS):
            suggestions.append({
                "priority": "OVERSEAS",
                "query": overseas_query,
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
                "source": "serpapi",
            })
    elif SERPAPI_SUPPLIER_FALLBACK and not SERPAPI_API_KEY:
        print("⚠️ [NON-STANDARD] SerpAPI fallback enabled but SERPAPI_API_KEY not set.")

    if TAVILY_SUPPLIER_FALLBACK and TAVILY_API_KEY:
        tavily_query = f"{search_text} supplier Malaysia"
        print(f"🌐 [NON-STANDARD] Fallback web search (Tavily): {tavily_query}")
        for row in tavily_search(tavily_query):
            row["source"] = "tavily"
            suggestions.append(row)

    return _dedupe_suggestions(suggestions)


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
            "part_no": _item_lookup_key(item),
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


def tavily_search(query: str, max_results: int = MAX_TAVILY_RESULTS) -> list[dict]:
    if not TAVILY_API_KEY:
        return []

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=TAVILY_API_KEY)
        result = client.search(query=query, search_depth="basic", max_results=max_results)
    except Exception as exc:
        print(f"⚠️ [NON-STANDARD] Tavily search error: {exc}")
        return []

    rows = []
    for hit in result.get("results", [])[:max_results]:
        rows.append({
            "priority": "TAVILY",
            "query": query,
            "title": hit.get("title", ""),
            "url": hit.get("url", ""),
            "snippet": str(hit.get("content", "") or "")[:320],
        })
    return rows


def search_supplier_suggestions(item):
    if not SEARCH_ENABLED:
        return []

    suggestions = copilot_search_suppliers(item)
    if suggestions:
        print(
            f"✅ [NON-STANDARD] Copilot returned {len(suggestions)} supplier(s) "
            f"for {_item_lookup_key(item)[:80]}"
        )
        return suggestions

    print(
        f"⚠️ [NON-STANDARD] Copilot empty/malformed for {_item_lookup_key(item)[:80]} "
        "— trying SerpAPI/Tavily fallback"
    )
    return _fallback_serpapi_tavily_suggestions(item)


def gather_supplier_suggestions(items: list[dict]) -> dict[str, list[dict]]:
    """Run Copilot (primary) with SerpAPI/Tavily fallback for each non-standard item."""
    all_suggestions: dict[str, list[dict]] = {}
    for item in items or []:
        key = _item_lookup_key(item)
        if not key:
            continue
        all_suggestions[key] = search_supplier_suggestions(item)
    return all_suggestions


def format_suggestions_html(all_suggestions: dict[str, list[dict]], items: list[dict]) -> str:
    """HTML block for monitor / customer ack emails."""
    if not items:
        return ""

    blocks = [
        "<hr>",
        "<p><strong>Supplier search suggestions (Copilot, SerpAPI/Tavily fallback):</strong></p>",
    ]

    for item in items:
        key = _item_lookup_key(item)
        suggestions = all_suggestions.get(key, [])
        label = key[:200] or "Item"
        qty = item.get("qty", "")
        blocks.append(f"<p><strong>{label}</strong> — Qty: {qty}</p>")

        if not suggestions:
            blocks.append("<p><em>No search results — check API keys or search manually.</em></p>")
            continue

        blocks.append("<ul>")
        for s in suggestions[:10]:
            title = s.get("title", "") or s.get("brand", "") or s.get("url", "")
            url = s.get("url", "") or s.get("website", "")
            snippet = str(s.get("snippet", "") or "")[:240]
            priority = s.get("priority", "")
            source = s.get("source", "")
            source_tag = f" / {source}" if source else ""
            blocks.append(
                f"<li><b>{priority}{source_tag}</b> "
                f"<a href=\"{url}\">{title}</a><br>{snippet}</li>"
            )
        blocks.append("</ul>")

    return "\n".join(blocks)


def format_suggestions_plain(all_suggestions: dict[str, list[dict]], items: list[dict]) -> str:
    """Plain-text block for WhatsApp monitor alerts."""
    if not items:
        return ""

    lines = ["", "Supplier search suggestions (Copilot, SerpAPI/Tavily fallback):", ""]
    for item in items:
        key = _item_lookup_key(item)
        suggestions = all_suggestions.get(key, [])
        lines.append(f"• {key[:160]} — Qty: {item.get('qty', '')}")
        if not suggestions:
            lines.append("  (no results)")
            continue
        for s in suggestions[:5]:
            lines.append(f"  - [{s.get('priority', '')}] {s.get('title', '')}")
            lines.append(f"    {s.get('url', '')}")
        lines.append("")
    return "\n".join(lines).strip()


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


def _priority_label(priority: str) -> str:
    return _PRIORITY_LABELS.get(str(priority or "").strip(), str(priority or "Other"))


def _normalize_supplier_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    if not re.match(r"^https?://", value, re.I):
        value = f"https://{value}"
    return value


def _is_valid_url_shape(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s/]+", str(url or "").strip(), re.I))


def verify_supplier_link(url: str) -> dict:
    """
    Test whether a supplier URL is reachable before including it in emails.
    Returns: {ok, status_code, final_url, error}
    """
    normalized = _normalize_supplier_url(url)
    if not normalized:
        return {"ok": False, "status_code": 0, "final_url": "", "error": "empty url"}
    if not _is_valid_url_shape(normalized):
        return {"ok": False, "status_code": 0, "final_url": normalized, "error": "invalid url format"}

    if normalized in _link_verify_cache:
        return _link_verify_cache[normalized]

    if not LINK_VERIFY_ENABLED:
        result = {"ok": True, "status_code": 0, "final_url": normalized, "error": ""}
        _link_verify_cache[normalized] = result
        return result

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; OpenClaw/1.0; +https://robomatics.sg)"
        ),
    }
    result = {"ok": False, "status_code": 0, "final_url": normalized, "error": ""}

    for method in ("head", "get"):
        try:
            if method == "head":
                response = requests.head(
                    normalized,
                    allow_redirects=True,
                    timeout=LINK_VERIFY_TIMEOUT,
                    headers=headers,
                )
            else:
                response = requests.get(
                    normalized,
                    allow_redirects=True,
                    timeout=LINK_VERIFY_TIMEOUT,
                    headers=headers,
                    stream=True,
                )
                response.close()
            status = int(response.status_code)
            final_url = str(response.url or normalized)
            result.update({
                "ok": 200 <= status < 400,
                "status_code": status,
                "final_url": final_url,
                "error": "" if 200 <= status < 400 else f"HTTP {status}",
            })
            if result["ok"] or status not in (405, 501):
                break
        except requests.RequestException as exc:
            result["error"] = str(exc)[:160]

    _link_verify_cache[normalized] = result
    if result["ok"]:
        print(f"   ✅ Link verified: {result['final_url']} ({result['status_code']})")
    else:
        print(f"   ⚠️ Link failed: {normalized} — {result['error'] or result['status_code']}")
    return result


def enrich_suggestions_with_link_checks(suggestions: list[dict]) -> list[dict]:
    """Attach link verification metadata; keep only reachable links for presentation."""
    enriched: list[dict] = []
    for row in suggestions or []:
        url = str(row.get("url") or row.get("website") or "").strip()
        check = verify_supplier_link(url)
        merged = {**row, "link_check": check, "link_ok": bool(check.get("ok"))}
        if merged["link_ok"]:
            merged["url"] = check.get("final_url") or _normalize_supplier_url(url)
            enriched.append(merged)
    return enriched


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _build_item_summary_row(idx: int, item: dict) -> str:
    key = _item_lookup_key(item)
    brand = item.get("brand", "UNKNOWN")
    qty = item.get("qty", "")
    reason = item.get("reason") or "Not found in warehouse / OBM product master"
    return (
        "<tr>"
        f"<td align='center'>{idx}</td>"
        f"<td>{_esc(brand)}</td>"
        f"<td>{_esc(key)}</td>"
        f"<td align='center'>{_esc(qty)}</td>"
        f"<td>{_esc(reason)}</td>"
        "</tr>"
    )


def _build_supplier_table_for_item(verified: list[dict], raw_count: int) -> str:
    if not verified:
        if raw_count:
            return (
                "<p><em>Supplier candidates were found but none passed link verification. "
                "Please search manually.</em></p>"
            )
        return "<p><em>No supplier suggestions returned.</em></p>"

    rows = []
    for idx, row in enumerate(verified[:MAX_TECHNICAL_SUPPLIERS_PER_ITEM], start=1):
        title = row.get("title") or row.get("brand") or "Supplier"
        url = row.get("url") or ""
        priority = _priority_label(row.get("priority", ""))
        source = str(row.get("source") or "").strip()
        snippet = str(row.get("snippet") or "").strip()[:220]
        status = row.get("link_check", {})
        status_code = status.get("status_code") or ""
        status_label = f"OK ({status_code})" if row.get("link_ok") else "Failed"

        rows.append(
            "<tr>"
            f"<td align='center'>{idx}</td>"
            f"<td>{_esc(priority)}</td>"
            f"<td>{_esc(title)}</td>"
            f"<td><a href='{_esc(url)}' target='_blank' rel='noopener'>{_esc(url)}</a></td>"
            f"<td align='center'>{_esc(source or '-')}</td>"
            f"<td align='center' style='color:green;'><b>{_esc(status_label)}</b></td>"
            f"<td>{_esc(snippet)}</td>"
            "</tr>"
        )

    return (
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;width:100%;font-size:13px;'>"
        "<tr style='background:#f2f2f2;'>"
        "<th>#</th><th>Region</th><th>Supplier</th><th>Verified Website</th>"
        "<th>Source</th><th>Link Test</th><th>Notes</th>"
        "</tr>"
        + "".join(rows)
        + "</table>"
    )


def build_technical_email_body(
    customer_name,
    customer_contact,
    channel,
    items,
    source_message,
    all_suggestions,
    verified_suggestions=None,
):
    item_rows = []
    supplier_sections = []
    total_verified = 0
    verified_map = verified_suggestions if verified_suggestions is not None else all_suggestions

    for idx, item in enumerate(items, start=1):
        key = _item_lookup_key(item)
        brand = item.get("brand", "UNKNOWN")
        qty = item.get("qty", "")
        reason = item.get("reason") or "Not found in warehouse / OBM product master"
        raw_suggestions = all_suggestions.get(key, [])
        verified = verified_map.get(key, [])
        total_verified += len(verified)

        item_rows.append(_build_item_summary_row(idx, item))
        supplier_sections.append(
            f"<h3 style='margin:18px 0 8px 0;'>Item {idx}: {_esc(key)}</h3>"
            f"<p><b>Brand:</b> {_esc(brand)} &nbsp;|&nbsp; "
            f"<b>Qty:</b> {_esc(qty)} &nbsp;|&nbsp; "
            f"<b>Routing reason:</b> {_esc(reason)}</p>"
            f"<p><b>Verified supplier links:</b> {len(verified)} of {len(raw_suggestions)} candidate(s)</p>"
            + _build_supplier_table_for_item(verified, len(raw_suggestions))
        )

    return f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;">
    <p>Hi Technical Team,</p>

    <p>OpenClaw detected <b>{len(items)}</b> non-standard item(s) that could not be quoted automatically
    from warehouse stock or OBM. Please review the specifications, confirm sourcing options, and advise
    pricing and lead time.</p>

    <table border="1" cellpadding="6" cellspacing="0"
           style="border-collapse:collapse;width:100%;background:#fafafa;">
        <tr><td colspan="2" style="background:#e8f0fe;"><b>Inquiry Summary</b></td></tr>
        <tr><td width="180"><b>Customer</b></td><td>{_esc(customer_name or '-')}</td></tr>
        <tr><td><b>Contact</b></td><td>{_esc(customer_contact or '-')}</td></tr>
        <tr><td><b>Channel</b></td><td>{_esc(channel or '-')}</td></tr>
        <tr><td><b>Created</b></td><td>{_esc(now_iso())}</td></tr>
        <tr><td><b>Items to source</b></td><td>{len(items)}</td></tr>
        <tr><td><b>Verified supplier links</b></td><td>{total_verified}</td></tr>
    </table>

    <h3 style="margin:20px 0 8px 0;">Items Requiring Technical Review</h3>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;">
        <tr style="background:#f2f2f2;">
            <th>No.</th>
            <th>Brand</th>
            <th>Part No / Description</th>
            <th>Qty</th>
            <th>Why Routed</th>
        </tr>
        {''.join(item_rows)}
    </table>

    <h3 style="margin:24px 0 8px 0;">Supplier Research (link-tested)</h3>
    <p style="color:#555;">
        Only supplier websites that responded successfully during an HTTP check are shown as clickable links.
        Please still confirm product fit, MOQ, and lead time with the supplier before quoting the customer.
    </p>
    {''.join(supplier_sections)}

    <h3 style="margin:24px 0 8px 0;">Original Customer Message</h3>
    <pre style="background:#f7f7f7;padding:12px;border:1px solid #ddd;white-space:pre-wrap;">{_esc(source_message)}</pre>

    <p>Regards,<br>OpenClaw Automation</p>
    </div>
    """


def send_to_technical(
    customer_name,
    customer_contact,
    channel,
    items,
    source_message,
    all_suggestions,
    verified_suggestions=None,
):
    mailbox = get_mailbox()

    if not mailbox:
        return False

    verified_map = verified_suggestions if verified_suggestions is not None else all_suggestions

    msg = mailbox.new_message()
    msg.to.add(TECHNICAL_EMAIL)
    msg.subject = f"Non-Standard Sourcing Request - {customer_name or customer_contact}"
    msg.body = build_technical_email_body(
        customer_name,
        customer_contact,
        channel,
        items,
        source_message,
        all_suggestions,
        verified_suggestions=verified_map,
    )
    msg.body_type = "html"
    msg.send()

    print("📧 [NON-STANDARD] Technical email sent.")
    print(f"   To: {TECHNICAL_EMAIL}")
    print(f"   Items: {len(items)}")
    verified_count = sum(len(v or []) for v in (verified_map or {}).values())
    print(f"   Verified supplier links included: {verified_count}")

    return True


def handle_non_standard_items(
    customer_name,
    customer_contact,
    channel,
    items,
    source_message,
    all_suggestions=None,
):
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

    if all_suggestions is None:
        all_suggestions = gather_supplier_suggestions(items)

    verified_suggestions: dict[str, list[dict]] = {}
    for item in items:
        key = _item_lookup_key(item)
        raw = all_suggestions.get(key, [])
        verified_suggestions[key] = enrich_suggestions_with_link_checks(raw)

    for item in items:
        key = _item_lookup_key(item)
        suggestions = verified_suggestions.get(key, [])

        save_non_standard_item(
            customer_name=customer_name,
            customer_contact=customer_contact,
            channel=channel,
            item=item,
            source_message=source_message,
            suggestions=suggestions
        )

        print(f"   📝 Logged non-standard item: {key[:80]} | Qty: {item.get('qty')}")

    email_sent = send_to_technical(
        customer_name=customer_name,
        customer_contact=customer_contact,
        channel=channel,
        items=items,
        source_message=source_message,
        all_suggestions=all_suggestions,
        verified_suggestions=verified_suggestions,
    )

    return {
        "handled": True,
        "technical_email_sent": email_sent,
        "items_count": len(items),
        "all_suggestions": all_suggestions,
        "verified_suggestions": verified_suggestions,
    }