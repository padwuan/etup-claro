import json
import os
import re
import statistics
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
PRODUCTS_PATH = ROOT / "data" / "produtos.json"
PRICES_PATH = ROOT / "data" / "precos.json"
API_URL = "https://serpapi.com/search.json"
MINIMUM_OFFERS = 3
MINIMUM_STORES = 2
MAX_RESULTS = 40


def normalize(value):
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.casefold()
    normalized = re.sub(r"(\d)\s+(gb|tb|mb|mhz)\b", r"\1\2", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def matching_model(title, models, excluded_phrases=()):
    normalized_title = normalize(title)
    if any(normalize(phrase) in normalized_title for phrase in excluded_phrases):
        return None
    return next((model for model in models if normalize(model) in normalized_title), None)


def parse_offer(item, models, excluded_phrases=()):
    title = item.get("title")
    model = matching_model(title or "", models, excluded_phrases)
    if not model:
        return None

    price = item.get("extracted_price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        return None

    source = item.get("source")
    link = item.get("product_link") or item.get("link")
    if not isinstance(source, str) or not source.strip() or not isinstance(link, str):
        return None
    if urlparse(link).scheme != "https" or not urlparse(link).netloc:
        return None

    return {
        "title": title.strip()[:240],
        "model": model,
        "store": source.strip()[:120],
        "price": round(float(price), 2),
        "url": link,
    }


def collect_offers(search_results, models, excluded_phrases=()):
    offers_by_url = {}
    for item in search_results:
        if not isinstance(item, dict):
            continue
        offer = parse_offer(item, models, excluded_phrases)
        if offer:
            offers_by_url.setdefault(offer["url"], offer)
    return sorted(offers_by_url.values(), key=lambda offer: (offer["price"], offer["store"].casefold()))


def build_product_result(product, search_results, checked_at):
    candidates = collect_offers(search_results, product["models"], product.get("excluded_phrases", []))
    offers_by_model = {}
    for offer in candidates:
        offers_by_model.setdefault(offer["model"], []).append(offer)

    offers = []
    model_summaries = []
    for model, model_offers in offers_by_model.items():
        stores = {offer["store"].casefold() for offer in model_offers}
        if len(model_offers) < MINIMUM_OFFERS or len(stores) < MINIMUM_STORES:
            continue
        prices = [offer["price"] for offer in model_offers]
        offers.extend(model_offers)
        model_summaries.append({
            "model": model,
            "offer_count": len(model_offers),
            "store_count": len(stores),
            "minimum_price": min(prices),
            "maximum_price": max(prices),
            "median_price": round(statistics.median(prices), 2),
        })

    offers.sort(key=lambda offer: (offer["price"], offer["store"].casefold()))
    stores = {offer["store"].casefold() for offer in offers}
    verified = bool(model_summaries)
    result = {
        "id": product["id"],
        "status": "verified" if verified else "insufficient_data",
        "checked_at": checked_at,
        "offer_count": len(offers),
        "store_count": len(stores),
        "offers": offers[:MAX_RESULTS],
        "models": model_summaries,
    }
    if verified:
        result["minimum_price"] = offers[0]["price"]
        result["maximum_price"] = offers[-1]["price"]
    return result


def update_history(result, previous_product, checked_at):
    history = previous_product.get("history", []) if previous_product else []
    if not isinstance(history, list):
        history = []
    history = [entry for entry in history if isinstance(entry, dict) and isinstance(entry.get("date"), str)]

    if result["status"] == "verified":
        today = checked_at[:10]
        previous_history = [entry for entry in history if entry["date"] < today]
        for model in result["models"]:
            previous_model = next(
                (
                    model_entry
                    for entry in reversed(previous_history)
                    for model_entry in entry.get("models", [])
                    if model_entry.get("model") == model["model"]
                ),
                None,
            )
            if previous_model:
                previous_median = previous_model.get("median_price")
                if isinstance(previous_median, (int, float)) and previous_median > 0:
                    difference = round(model["median_price"] - previous_median, 2)
                    model["change_since_previous"] = difference
                    model["change_percent_since_previous"] = round(difference / previous_median * 100, 2)
                    model["compared_to"] = next(
                        entry["date"]
                        for entry in reversed(previous_history)
                        if any(
                            model_entry.get("model") == model["model"]
                            for model_entry in entry.get("models", [])
                        )
                    )

        today_entry = {
            "date": today,
            "models": [
                {
                    "model": model["model"],
                    "minimum_price": model["minimum_price"],
                    "maximum_price": model["maximum_price"],
                    "median_price": model["median_price"],
                    "offer_count": model["offer_count"],
                    "store_count": model["store_count"],
                }
                for model in result["models"]
            ],
        }
        history = [entry for entry in history if entry["date"] != today]
        history.append(today_entry)

    result["history"] = history[-90:]
    return result


def search_product(product, api_key):
    params = urlencode({
        "engine": "google_shopping",
        "q": product["query"],
        "google_domain": "google.com.br",
        "gl": "br",
        "hl": "pt-br",
        "api_key": api_key,
    })
    request = Request(
        f"{API_URL}?{params}",
        headers={"Accept": "application/json", "User-Agent": "SetupClaroPriceMonitor/1.0"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Falha na consulta de {product['id']}: {error}") from error

    if not isinstance(payload, dict) or payload.get("error"):
        detail = payload.get("error", "resposta inválida") if isinstance(payload, dict) else "resposta inválida"
        raise RuntimeError(f"Falha na consulta de {product['id']}: {detail}")
    results = payload.get("shopping_results", [])
    if not isinstance(results, list):
        raise RuntimeError(f"Resposta inválida da SerpApi para {product['id']}: shopping_results não é uma lista.")
    return results


def write_json_atomically(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_path = Path(temporary_file.name)
        temporary_path.replace(path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def update_prices():
    api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("SERPAPI_API_KEY não está configurada. Cadastre-a como Secret do GitHub Actions.")

    with PRODUCTS_PATH.open(encoding="utf-8") as file:
        products = json.load(file)
    if not isinstance(products, list):
        raise RuntimeError("data/produtos.json precisa conter uma lista de produtos.")

    previous_products = {}
    if PRICES_PATH.exists():
        with PRICES_PATH.open(encoding="utf-8") as file:
            previous_data = json.load(file)
        previous_products = {
            product["id"]: product
            for product in previous_data.get("products", [])
            if isinstance(product, dict) and isinstance(product.get("id"), str)
        }

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    results = []
    for product in products:
        if not product.get("enabled"):
            result = {
                "id": product["id"],
                "status": "skipped",
                "checked_at": checked_at,
                "reason": product.get("disabled_reason", "Produto desativado na configuração."),
                "offer_count": 0,
                "store_count": 0,
                "offers": [],
                "models": [],
            }
            results.append(update_history(result, previous_products.get(product["id"]), checked_at))
            print(f"Ignorado {product['id']}: {product.get('disabled_reason', 'desativado')}")
            continue

        print(f"Consultando {product['id']}...")
        search_results = search_product(product, api_key)
        result = build_product_result(product, search_results, checked_at)
        result = update_history(result, previous_products.get(product["id"]), checked_at)
        results.append(result)
        print(
            f"{product['id']}: {result['status']}; "
            f"{result['offer_count']} anúncios; {result['store_count']} lojas."
        )

    payload = {
        "updated_at": checked_at,
        "provider": "Google Shopping via SerpApi",
        "products": results,
    }
    write_json_atomically(PRICES_PATH, payload)


if __name__ == "__main__":
    try:
        update_prices()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Erro ao atualizar preços: {error}", file=sys.stderr)
        raise SystemExit(1) from error
