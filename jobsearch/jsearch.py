"""Thin client for the OpenWeb Ninja JSearch API (Google for Jobs results)."""

import os

import requests

BASE_URL = "https://api.openwebninja.com/jsearch/search"
DATE_POSTED_OPTIONS = ["all", "today", "3days", "week", "month"]
EMPLOYMENT_TYPES = ["FULLTIME", "CONTRACTOR", "PARTTIME", "INTERN"]
MAX_PAGES = 3  # each page is ~10 jobs and counts against the monthly quota


class JSearchError(Exception):
    pass


def build_query(query, location=None):
    """JSearch takes location as part of the query text, e.g. 'java developer in Chicago'."""
    query = query.strip()
    if location and location.strip():
        query = f"{query} in {location.strip()}"
    return query


def search(query, location=None, remote_only=False, date_posted="all",
           employment_types=None, num_pages=1, api_key=None):
    """Run one search. Returns (results, pages_requested)."""
    api_key = api_key or os.getenv("JSEARCH_API_KEY")
    if not api_key:
        raise JSearchError("Missing JSEARCH_API_KEY in .env")

    num_pages = max(1, min(int(num_pages or 1), MAX_PAGES))
    params = {
        "query": build_query(query, location),
        "page": 1,
        "num_pages": num_pages,
        "country": "us",
        "date_posted": date_posted if date_posted in DATE_POSTED_OPTIONS else "all",
    }
    if remote_only:
        params["work_from_home"] = "true"
    if employment_types:
        params["employment_types"] = employment_types

    try:
        resp = requests.get(BASE_URL, params=params, headers={"x-api-key": api_key}, timeout=60)
    except requests.RequestException as e:
        raise JSearchError(f"Could not reach JSearch: {e}") from e

    if resp.status_code in (401, 403):
        raise JSearchError("JSearch rejected the API key (check JSEARCH_API_KEY in .env)")
    if resp.status_code == 429:
        raise JSearchError("JSearch quota or rate limit reached; try again later")
    if resp.status_code != 200:
        raise JSearchError(f"JSearch returned HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    if body.get("status") != "OK":
        raise JSearchError(f"JSearch error: {body.get('error') or body.get('message') or body}")
    return body.get("data") or [], num_pages
