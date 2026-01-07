# websearch.py — Free DuckDuckGo Search Helper
import requests

def ddg_search(query, n=5):
    """Free DuckDuckGo instant search (no API key needed)."""
    url = "https://duckduckgo.com/?q={}&format=json&no_redirect=1".format(
        requests.utils.quote(query)
    )
    try:
        r = requests.get(url, timeout=6)
        if r.status_code != 200:
            return ["Search error: could not reach DuckDuckGo"]

        data = r.json()
        results = []

        # RelatedTopics usually contains basic page links
        for item in data.get("RelatedTopics", []):
            if "FirstURL" in item and "Text" in item:
                results.append(f"{item['Text']} — {item['FirstURL']}")
                if len(results) >= n:
                    break

        if not results:
            return ["No results found (DuckDuckGo returned empty)."]

        return results
    except Exception as e:
        return [f"Search exception: {str(e)}"]
