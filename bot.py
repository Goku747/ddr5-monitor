#!/usr/bin/env python3
"""
DDR5 RAM price and stock monitor.

Features:
- Monitors hardcoded product URLs
- Extracts product name, stock status, and price
- Sends Telegram alerts only when:
    1. Out of Stock -> In Stock
    2. Price drops while the product remains In Stock
- Persists state to docs/state.json
- Generates a responsive Tailwind CSS dashboard at docs/index.html
- Continues processing if an individual retailer fails
- Supports per-product CSS selectors and regular-expression fallbacks
- Uses HTTP retries, randomized browser headers, request pacing, and timeouts

Environment variables:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

The workflow commits generated files. This script itself does not run Git commands,
which keeps authentication and repository operations inside GitHub Actions.
"""

from __future__ import annotations

import html
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent
DOCS_DIR = ROOT_DIR / "docs"
STATE_FILE = DOCS_DIR / "state.json"
DASHBOARD_FILE = DOCS_DIR / "index.html"

REQUEST_TIMEOUT_SECONDS = 25
MIN_REQUEST_DELAY_SECONDS = 1.5
MAX_REQUEST_DELAY_SECONDS = 4.0

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) "
        "Gecko/20100101 Firefox/129.0"
    ),
]

# Replace the example URLs and selectors with real product pages.
#
# Selector strategy:
# - name_selectors: first matching selector becomes the displayed product name
# - price_selectors: first matching selector containing a valid price is used
# - in_stock_selectors: a matching visible element indicates in-stock
# - out_of_stock_selectors: a matching visible element indicates out-of-stock
#
# If selectors do not identify stock, textual stock phrases are checked.
#
# Amazon notes:
# - Product page markup can vary by geography and account state.
# - Pages may return a CAPTCHA or anti-automation response.
# - Update selectors only after inspecting the page you are permitted to access.
#
# Keep product identifiers stable. They are used as keys in state.json.
PRODUCTS: list[dict[str, Any]] = [
    {
        "id": "amazon-example-ddr5-8gb",
        "platform": "Amazon",
        "capacity": "8GB",
        "fallback_name": "Example DDR5 8GB RAM",
        "url": "https://www.amazon.in/REPLACE_WITH_PRODUCT_PATH",
        "currency": "INR",
        "name_selectors": [
            "#productTitle",
            "h1.a-size-large",
            "h1",
        ],
        "price_selectors": [
            ".a-price .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#corePrice_feature_div .a-offscreen",
        ],
        "in_stock_selectors": [
            "#availability .a-color-success",
        ],
        "out_of_stock_selectors": [
            "#availability .a-color-price",
        ],
        "in_stock_phrases": [
            "in stock",
            "available to ship",
        ],
        "out_of_stock_phrases": [
            "currently unavailable",
            "out of stock",
            "temporarily out of stock",
        ],
    },
    {
        "id": "mdcomputers-example-ddr5-16gb",
        "platform": "MDComputers",
        "capacity": "16GB",
        "fallback_name": "Example DDR5 16GB RAM",
        "url": "https://mdcomputers.in/REPLACE_WITH_PRODUCT_PATH",
        "currency": "INR",
        "name_selectors": [
            "h1.product-title",
            "h1",
        ],
        "price_selectors": [
            ".product-price-new",
            ".price-new",
            "[itemprop='price']",
        ],
        "in_stock_selectors": [
            ".stock.in-stock",
            ".product-stock",
        ],
        "out_of_stock_selectors": [
            ".stock.out-of-stock",
            ".text-out-of-stock",
        ],
        "in_stock_phrases": [
            "in stock",
            "add to cart",
        ],
        "out_of_stock_phrases": [
            "out of stock",
            "sold out",
        ],
    },
    {
        "id": "newegg-example-ddr5-16gb",
        "platform": "Newegg",
        "capacity": "16GB",
        "fallback_name": "Example DDR5 16GB RAM",
        "url": "https://www.newegg.com/REPLACE_WITH_PRODUCT_PATH",
        "currency": "USD",
        "name_selectors": [
            "h1.product-title",
            "h1",
        ],
        "price_selectors": [
            ".price-current",
            "[itemprop='price']",
        ],
        "in_stock_selectors": [
            ".product-inventory",
            "button.btn-primary",
        ],
        "out_of_stock_selectors": [
            ".product-inventory",
            ".btn-message",
        ],
        "in_stock_phrases": [
            "in stock",
            "add to cart",
        ],
        "out_of_stock_phrases": [
            "out of stock",
            "sold out",
            "auto notify",
        ],
    },
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ProductResult:
    product_id: str
    product_name: str
    platform: str
    capacity: str
    url: str
    currency: str
    price: Optional[str]
    stock_status: str
    checked_at: str
    successful: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def request_headers(product_url: str) -> dict[str, str\]:
    parsed = urlparse(product_url)

    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }


def detect_block_page(response: requests.Response) -> Optional[str\]:
    body = response.text.lower()

    block_markers = (
        "captcha",
        "robot check",
        "verify you are human",
        "access denied",
        "unusual traffic",
        "automated access",
        "temporarily blocked",
    )

    if response.status_code in (401, 403):
        return f"HTTP {response.status_code}: access denied"

    if response.status_code == 429:
        return "HTTP 429: retailer rate limit reached"

    if any(marker in body for marker in block_markers):
        return "Retailer returned a CAPTCHA or anti-automation page"

    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def clean_text(value: str) -> str:
    return " ".join(value.split()).strip()


def first_selector_text(
    soup: BeautifulSoup,
    selectors: list[str],
) -> Optional[str\]:
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            value = element.get("content") or element.get_text(" ", strip=True)
            value = clean_text(str(value))
            if value:
                return value

    return None


def selectors_text(
    soup: BeautifulSoup,
    selectors: list[str],
) -> list[str\]:
    values: list[str] = []

    for selector in selectors:
        for element in soup.select(selector):
            value = element.get("content") or element.get_text(" ", strip=True)
            value = clean_text(str(value))
            if value:
                values.append(value)

    return values


def decimal_to_storage(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def parse_price(raw_value: Optional[str]) -> Optional[Decimal\]:
    if not raw_value:
        return None

    normalized = raw_value.replace("\u00a0", " ").strip()

    # Keep digits and common separators. This supports strings such as:
    # ₹4,299.00
    # $79.99
    # INR 4,299
    matches = re.findall(r"\d[\d,]*(?:\.\d{1,2})?", normalized)

    for match in matches:
        candidate = match.replace(",", "")

        try:
            price = Decimal(candidate)
        except InvalidOperation:
            continue

        if price > 0:
            return price

    return None


def extract_json_ld_objects(soup: BeautifulSoup) -> list[dict[str, Any]\]:
    objects: list[dict[str, Any]] = []

    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text(strip=True)

        if not raw:
            continue

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        if isinstance(payload, dict):
            graph = payload.get("@graph")

            if isinstance(graph, list):
                objects.extend(item for item in graph if isinstance(item, dict))

            objects.append(payload)

        elif isinstance(payload, list):
            objects.extend(item for item in payload if isinstance(item, dict))

    return objects


def extract_json_ld_product(
    soup: BeautifulSoup,
) -> tuple[Optional[str], Optional[Decimal], Optional[str]\]:
    for item in extract_json_ld_objects(soup):
        item_type = item.get("@type", "")

        if isinstance(item_type, list):
            is_product = "Product" in item_type
        else:
            is_product = item_type == "Product"

        if not is_product:
            continue

        name = clean_text(str(item.get("name", ""))) or None
        offers = item.get("offers")

        if isinstance(offers, list):
            offers = next(
                (offer for offer in offers if isinstance(offer, dict)),
                {},
            )

        if not isinstance(offers, dict):
            offers = {}

        price = parse_price(str(offers.get("price", "")))
        availability = str(offers.get("availability", "")).lower() or None

        return name, price, availability

    return None, None, None


def infer_stock_status(
    soup: BeautifulSoup,
    product: dict[str, Any],
    json_ld_availability: Optional[str],
) -> str:
    out_selector_texts = selectors_text(
        soup,
        product.get("out_of_stock_selectors", []),
    )
    in_selector_texts = selectors_text(
        soup,
        product.get("in_stock_selectors", []),
    )

    out_phrases = [
        phrase.lower()
        for phrase in product.get("out_of_stock_phrases", [])
    ]
    in_phrases = [
        phrase.lower()
        for phrase in product.get("in_stock_phrases", [])
    ]

    out_selector_block = " ".join(out_selector_texts).lower()
    in_selector_block = " ".join(in_selector_texts).lower()

    # Explicit out-of-stock phrases take priority because some product pages
    # keep disabled "Add to cart" elements in their markup.
    if any(phrase in out_selector_block for phrase in out_phrases):
        return "out_of_stock"

    if any(phrase in in_selector_block for phrase in in_phrases):
        return "in_stock"

    if json_ld_availability:
        availability = json_ld_availability.lower()

        if "outofstock" in availability or "soldout" in availability:
            return "out_of_stock"

        if (
            "instock" in availability
            or "limitedavailability" in availability
            or "onlineonly" in availability
        ):
            return "in_stock"

    page_text = clean_text(soup.get_text(" ", strip=True)).lower()

    # Search out-of-stock indicators before in-stock indicators.
    if any(phrase in page_text for phrase in out_phrases):
        return "out_of_stock"

    if any(phrase in page_text for phrase in in_phrases):
        return "in_stock"

    return "unknown"


def scrape_product(
    session: requests.Session,
    product: dict[str, Any],
) -> ProductResult:
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        response = session.get(
            product["url"],
            headers=request_headers(product["url"]),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        block_reason = detect_block_page(response)
        if block_reason:
            raise RuntimeError(block_reason)

        soup = BeautifulSoup(response.text, "html.parser")

        json_name, json_price, json_availability = (
            extract_json_ld_product(soup)
        )

        selector_name = first_selector_text(
            soup,
            product.get("name_selectors", []),
        )

        product_name = (
            json_name
            or selector_name
            or product.get("fallback_name")
            or product["id"]
        )

        price: Optional[Decimal] = None

        for raw_price in selectors_text(
            soup,
            product.get("price_selectors", []),
        ):
            price = parse_price(raw_price)
            if price is not None:
                break

        if price is None:
            price = json_price

        stock_status = infer_stock_status(
            soup,
            product,
            json_availability,
        )

        return ProductResult(
            product_id=product["id"],
            product_name=product_name,
            platform=product["platform"],
            capacity=product["capacity"],
            url=product["url"],
            currency=product.get("currency", "INR"),
            price=decimal_to_storage(price) if price is not None else None,
            stock_status=stock_status,
            checked_at=checked_at,
            successful=True,
            error=None,
        )

    except requests.RequestException as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    logging.error(
        "Failed to scrape %s (%s): %s",
        product.get("fallback_name", product["id"]),
        product["url"],
        error,
    )

    return ProductResult(
        product_id=product["id"],
        product_name=product.get("fallback_name", product["id"]),
        platform=product["platform"],
        capacity=product["capacity"],
        url=product["url"],
        currency=product.get("currency", "INR"),
        price=None,
        stock_status="unknown",
        checked_at=checked_at,
        successful=False,
        error=error,
    )


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any\]:
    if not STATE_FILE.exists():
        return {
            "schema_version": 1,
            "updated_at": None,
            "products": {},
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as state_handle:
            state = json.load(state_handle)

        if not isinstance(state, dict):
            raise ValueError("State root must be a JSON object")

        state.setdefault("schema_version", 1)
        state.setdefault("updated_at", None)
        state.setdefault("products", {})

        if not isinstance(state["products"], dict):
            raise ValueError("State products value must be a JSON object")

        return state

    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logging.error("Could not read state file: %s", exc)

        # Preserve the broken file for investigation.
        broken_path = STATE_FILE.with_suffix(".json.broken")

        try:
            STATE_FILE.replace(broken_path)
            logging.error("Broken state moved to %s", broken_path)
        except OSError:
            pass

        return {
            "schema_version": 1,
            "updated_at": None,
            "products": {},
        }


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", encoding="utf-8", newline="\n") as file_handle:
        file_handle.write(content)

    temp_path.replace(path)


def save_state(state: dict[str, Any]) -> None:
    serialized = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    atomic_write_text(STATE_FILE, serialized + "\n")


def previous_decimal(product_state: dict[str, Any]) -> Optional[Decimal\]:
    raw_price = product_state.get("price")

    if raw_price is None:
        return None

    try:
        return Decimal(str(raw_price))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def format_currency(price: Optional[str], currency: str) -> str:
    if price is None:
        return "Price unavailable"

    try:
        amount = Decimal(price)
    except InvalidOperation:
        return price

    symbols = {
        "INR": "₹",
        "USD": "$",
        "EUR": "€",
        "GBP": "£",
    }

    symbol = symbols.get(currency.upper())

    if symbol:
        return f"{symbol}{amount:,.2f}"

    return f"{currency.upper()} {amount:,.2f}"


def telegram_escape(value: str) -> str:
    return html.escape(value, quote=False)


def send_telegram_alert(
    session: requests.Session,
    result: ProductResult,
    reason: str,
    previous_price: Optional[Decimal] = None,
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning(
            "Telegram credentials are not configured; alert skipped for %s",
            result.product_name,
        )
        return False

    price_text = format_currency(result.price, result.currency)
    status_text = (
        "In Stock"
        if result.stock_status == "in_stock"
        else result.stock_status.replace("_", " ").title()
    )

    reason_text = {
        "restock": "✅ Back in stock",
        "price_drop": "📉 Price drop while in stock",
    }.get(reason, "🔔 Product update")

    lines = [
        f"<b>{telegram_escape(reason_text)}</b>",
        "",
        f"<b>Product:</b> {telegram_escape(result.product_name)}",
        f"<b>Platform:</b> {telegram_escape(result.platform)}",
        f"<b>Capacity:</b> {telegram_escape(result.capacity)}",
        f"<b>Current Price:</b> {telegram_escape(price_text)}",
        f"<b>Stock Status:</b> {telegram_escape(status_text)}",
    ]

    if reason == "price_drop" and previous_price is not None:
        previous_text = format_currency(
            decimal_to_storage(previous_price),
            result.currency,
        )
        lines.append(
            f"<b>Previous Price:</b> {telegram_escape(previous_text)}"
        )

    lines.extend(
        [
            "",
            f'{html.escape(result.url, quote=True)}'
            "View product</a>",
        ]
    )

    endpoint = (
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = session.post(
            endpoint,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

        response_payload = response.json()
        if not response_payload.get("ok"):
            raise RuntimeError(
                response_payload.get("description", "Unknown Telegram error")
            )

        logging.info(
            "Telegram alert sent for %s: %s",
            result.product_name,
            reason,
        )
        return True

    except (requests.RequestException, ValueError, RuntimeError) as exc:
        logging.error(
            "Telegram alert failed for %s: %s",
            result.product_name,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Notification decisions
# ---------------------------------------------------------------------------

def evaluate_notification(
    result: ProductResult,
    previous: Optional[dict[str, Any]],
) -> tuple[Optional[str], Optional[Decimal]\]:
    # No first-run alert. The requirement is a transition from a previously
    # observed state, not merely discovering an item already in stock.
    if previous is None:
        return None, None

    # Failed or unknown results must not create false restock alerts.
    if not result.successful or result.stock_status == "unknown":
        return None, None

    previous_status = previous.get("stock_status")

    if (
        previous_status == "out_of_stock"
        and result.stock_status == "in_stock"
    ):
        return "restock", previous_decimal(previous)

    if (
        previous_status == "in_stock"
        and result.stock_status == "in_stock"
        and result.price is not None
    ):
        old_price = previous_decimal(previous)

        if old_price is not None:
            new_price = Decimal(result.price)

            if new_price < old_price:
                return "price_drop", old_price

    return None, None


def merge_result_into_state(
    result: ProductResult,
    previous: Optional[dict[str, Any]],
) -> dict[str, Any\]:
    current = asdict(result)

    # If a scrape fails, retain the last reliable price and stock status for
    # display while recording the latest error and failed check timestamp.
    if not result.successful and previous:
        current["price"] = previous.get("price")
        current["stock_status"] = previous.get(
            "stock_status",
            "unknown",
        )
        current["last_successful_check"] = previous.get(
            "last_successful_check"
        )
    else:
        current["last_successful_check"] = result.checked_at

    return current


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def stock_badge(status: str) -> tuple[str, str, str\]:
    if status == "in_stock":
        return (
            "In Stock",
            "bg-emerald-100 text-emerald-800 ring-emerald-600/20",
            "bg-emerald-500",
        )

    if status == "out_of_stock":
        return (
            "Out of Stock",
            "bg-red-100 text-red-800 ring-red-600/20",
            "bg-red-500",
        )

    return (
        "Unknown",
        "bg-amber-100 text-amber-800 ring-amber-600/20",
        "bg-amber-500",
    )


def safe(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def generate_dashboard(
    state: dict[str, Any],
    generated_at: datetime,
) -> None:
    products = list(state.get("products", {}).values())

    products.sort(
        key=lambda product: (
            product.get("capacity", ""),
            product.get("platform", ""),
            product.get("product_name", ""),
        )
    )

    in_stock_count = sum(
        1
        for product in products
        if product.get("stock_status") == "in_stock"
    )

    out_of_stock_count = sum(
        1
        for product in products
        if product.get("stock_status") == "out_of_stock"
    )

    unknown_count = len(products) - in_stock_count - out_of_stock_count

    card_fragments: list[str] = []

    for product in products:
        status = product.get("stock_status", "unknown")
        badge_text, badge_classes, dot_class = stock_badge(status)
        price_text = format_currency(
            product.get("price"),
            product.get("currency", "INR"),
        )

        error = product.get("error")
        error_html = ""

        if error:
            error_html = f"""
                <div class="mt-4 rounded-xl border border-amber-200
                            bg-amber-50 p-3 text-xs text-amber-800">
                    Latest check issue: {safe(error)}
                </div>
            """

        card_fragments.append(
            f"""
            <article
                class="group flex h-full flex-col rounded-3xl border
                       border-slate-200 bg-white p-5 shadow-sm
                       transition duration-200 hover:-translate-y-1
                       hover:shadow-lg"
            >
                <div class="flex items-start justify-between gap-4">
                    <div>
                        <div class="mb-2 flex flex-wrap gap-2">
                            <span
                                class="rounded-full bg-indigo-50 px-3 py-1
                                       text-xs font-semibold text-indigo-700"
                            >
                                {safe(product.get("platform"))}
                            </span>
                            <span
                                class="rounded-full bg-slate-100 px-3 py-1
                                       text-xs font-semibold text-slate-700"
                            >
                                DDR5 {safe(product.get("capacity"))}
                            </span>
                        </div>

                        <h2
                            class="line-clamp-3 text-lg font-bold
                                   leading-snug text-slate-900"
                        >
                            {safe(product.get("product_name"))}
                        </h2>
                    </div>
                </div>

                <div class="mt-6 flex items-end justify-between gap-4">
                    <div>
                        <p
                            class="text-xs font-medium uppercase
                                   tracking-wider text-slate-500"
                        >
                            Current price
                        </p>
                        <p class="mt-1 text-2xl font-extrabold text-slate-950">
                            {safe(price_text)}
                        </p>
                    </div>

                    <span
                        class="inline-flex shrink-0 items-center gap-2
                               rounded-full px-3 py-1.5 text-xs font-semibold
                               ring-1 ring-inset {badge_classes}"
                    >
                        <span
                            class="h-2 w-2 rounded-full {dot_class}"
                        ></span>
                        {safe(badge_text)}
                    </span>
                </div>

                {error_html}

                <div class="mt-auto pt-6">
                    {safe(product.get(}"
                        target="_blank"
                        rel="noopener noreferrer nofollow"
                        class="inline-flex w-full items-center justify-center
                               rounded-xl bg-indigo-600 px-4 py-3
                               text-sm font-semibold text-white
                               transition hover:bg-indigo-500
                               focus:outline-none focus:ring-2
                               focus:ring-indigo-500 focus:ring-offset-2"
                    >
                        View product
                        <svg
                            class="ml-2 h-4 w-4"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke-width="2"
                            stroke="currentColor"
                            aria-hidden="true"
                        >
                            <path
                                stroke-linecap="round"
                                stroke-linejoin="round"
                                d="M13.5 4.5H19.5V10.5M19 5L10 14
                                   M8 6H5.5A1.5 1.5 0 0 0 4 7.5V18.5
                                   A1.5 1.5 0 0 0 5.5 20H16.5
                                   A1.5 1.5 0 0 0 18 18.5V16"
                            />
                        </svg>
                    </a>
                </div>
            </article>
            """
        )

    generated_display = generated_at.strftime("%d %B %Y, %H:%M:%S UTC")
    cards_html = "\n".join(card_fragments)

    document = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta
        name="description"
        content="DDR5 8GB and 16GB RAM price and availability dashboard"
    >
    <title>DDR5 RAM Monitor</title>

    https://cdn.tailwindcss.comscript>

    <style>
        body {{
            background:
                radial-gradient(
                    circle at top left,
                    rgba(99, 102, 241, 0.12),
                    transparent 32rem
                ),
                #f8fafc;
        }}
    </style>
</head>

<body class="min-h-screen text-slate-900">
    <main class="mx-auto max-w-7xl px-4 py-8 sm:px-6 lg:px-8 lg:py-12">
        <header
            class="overflow-hidden rounded-3xl bg-slate-950 px-6 py-8
                   text-white shadow-xl sm:px-10 sm:py-10"
        >
            <div
                class="flex flex-col justify-between gap-8
                       lg:flex-row lg:items-end"
            >
                <div>
                    <div
                        class="mb-4 inline-flex items-center gap-2
                               rounded-full bg-white/10 px-3 py-1
                               text-xs font-semibold text-indigo-200"
                    >
                        <span
                            class="h-2 w-2 animate-pulse rounded-full
                                   bg-emerald-400"
                        ></span>
                        Automated monitoring
                    </div>

                    <h1
                        class="max-w-3xl text-3xl font-black tracking-tight
                               sm:text-5xl"
                    >
                        DDR5 RAM Price and Stock Monitor
                    </h1>

                    <p class="mt-4 max-w-2xl text-sm text-slate-300 sm:text-base">
                        Compare availability and pricing for monitored
                        DDR5 8GB and 16GB memory sticks.
                    </p>
                </div>

                <div
                    class="rounded-2xl border border-white/10
                           bg-white/5 px-5 py-4"
                >
                    <p
                        class="text-xs font-semibold uppercase
                               tracking-widest text-slate-400"
                    >
                        Last updated
                    </p>
                    <time
                        class="mt-1 block text-sm font-semibold text-white"
                        datetime="{generated_at.isoformat()}"
                    >
                        {safe(generated_display)}
                    </time>
                </div>
            </div>
        </header>

        <section
            class="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4"
            aria-label="Monitoring summary"
        >
            <div class="rounded-2xl border border-slate-200 bg-white p-5">
                <p class="text-sm font-medium text-slate-500">
                    Monitored
                </p>
                <p class="mt-2 text-3xl font-black text-slate-950">
                    {len(products)}
                </p>
            </div>

            <div class="rounded-2xl border border-emerald-200 bg-emerald-50 p-5">
                <p class="text-sm font-medium text-emerald-700">
                    In stock
                </p>
                <p class="mt-2 text-3xl font-black text-emerald-900">
                    {in_stock_count}
                </p>
            </div>

            <div class="rounded-2xl border border-red-200 bg-red-50 p-5">
                <p class="text-sm font-medium text-red-700">
                    Out of stock
                </p>
                <p class="mt-2 text-3xl font-black text-red-900">
                    {out_of_stock_count}
                </p>
            </div>

            <div class="rounded-2xl border border-amber-200 bg-amber-50 p-5">
                <p class="text-sm font-medium text-amber-700">
                    Check issues
                </p>
                <p class="mt-2 text-3xl font-black text-amber-900">
                    {unknown_count}
                </p>
            </div>
        </section>

        <section
            class="mt-6 grid grid-cols-1 gap-5 md:grid-cols-2
                   xl:grid-cols-3"
            aria-label="Monitored products"
        >
            {cards_html}
        </section>

        <footer
            class="mt-10 border-t border-slate-200 pt-6
                   text-center text-xs text-slate-500"
        >
            Prices and availability may change before the retailer page loads.
            Verify the final price and stock status before purchasing.
        </footer>
    </main>
</body>
</html>
"""

    atomic_write_text(DASHBOARD_FILE, document)


# ---------------------------------------------------------------------------
# Main execution
# ---------------------------------------------------------------------------

def validate_configuration() -> None:
    ids = [product["id"] for product in PRODUCTS]

    if len(ids) != len(set(ids)):
        raise ValueError("Every product must have a unique id")

    required_keys = {
        "id",
        "platform",
        "capacity",
        "fallback_name",
        "url",
        "currency",
    }

    for product in PRODUCTS:
        missing = required_keys - product.keys()

        if missing:
            raise ValueError(
                f"Product {product.get('id',      f"required fields: {sorted(missing)}"
            )

        parsed = urlparse(product["url"])

        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"Product {product['id']} has an invalid URL"
            )


def main() -> int:
    configure_logging()

    try:
        validate_configuration()
    except ValueError as exc:
        logging.critical("Invalid configuration: %s", exc)
        return 2

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    session = build_session()
    run_started_at = datetime.now(timezone.utc)

    logging.info("Starting monitor for %d products", len(PRODUCTS))

    successful_checks = 0
    failed_checks = 0
    alert_failures = 0

    for index, product in enumerate(PRODUCTS):
        if index > 0:
            delay = random.uniform(
                MIN_REQUEST_DELAY_SECONDS,
                MAX_REQUEST_DELAY_SECONDS,
            )
            time.sleep(delay)

        logging.info(
            "Checking %s on %s",
            product["fallback_name"],
            product["platform"],
        )

        result = scrape_product(session, product)

        if result.successful:
            successful_checks += 1
        else:
            failed_checks += 1

        previous = state["products"].get(result.product_id)
        notification_reason, previous_price = evaluate_notification(
            result,
            previous,
        )

        if notification_reason:
            alert_sent = send_telegram_alert(
                session,
                result,
                notification_reason,
                previous_price,
            )

            if not alert_sent:
                alert_failures += 1

        state["products"][result.product_id] = merge_result_into_state(
            result,
            previous,
        )

    state["updated_at"] = run_started_at.isoformat()
    state["last_run"] = {
        "started_at": run_started_at.isoformat(),
        "successful_checks": successful_checks,
        "failed_checks": failed_checks,
        "alert_failures": alert_failures,
    }

    save_state(state)
    generate_dashboard(state, run_started_at)

    logging.info(
        "Monitoring completed: %d successful, %d failed, "
        "%d alert failures",
        successful_checks,
        failed_checks,
        alert_failures,
    )

    # A failed retailer should not prevent state/dashboard publication.
    # Return failure only when every configured product failed.
    if PRODUCTS and successful_checks == 0:
        logging.error("Every product check failed")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
