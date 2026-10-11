"""Trudvsem (Rabota Rossii) source adapter (docs/04 section 10).

Primary: open API opendata.trudvsem.ru without key (GET JSON).
Fallback: HTML of trudvsem.ru via curl_cffi, then AntiBanSession
(same curl_cffi -> Playwright chain as orchestrator).
"""
from __future__ import annotations

import asyncio
import html as html_lib
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

from app.modules.anti_ban import AntiBanSession
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.sources.base import BaseSourceAdapter

__all__ = [
    "API_BASE",
    "CARD_BASE",
    "LISTING_URL",
    "PAGE_SIZE",
    "TrudvsemAdapter",
    "TrudvsemAPIError",
    "api_url_for",
    "card_url_for",
    "extract_card_fields",
    "has_next_page",
    "listing_url_for",
    "parse_api_vacancies",
    "parse_html_jobs",
    "region_code_for",
]

logger = logging.getLogger(__name__)

API_BASE = "http://opendata.trudvsem.ru/api/v1/vacancies"
LISTING_URL = "https://trudvsem.ru/vacancy/search"
CARD_BASE = "https://trudvsem.ru/vacancy/card"
PAGE_SIZE = 100

_ALLOWED_HOSTS = ("trudvsem.ru", "www.trudvsem.ru", "opendata.trudvsem.ru")
_NOT_FOUND_STATUSES = (404, 410)

_REGION_NUMBERS: dict[str, str] = {
    "moskva": "77",
    "moscow": "77",
    "samarskaya oblast": "63",
    "samara": "63",
    "samarskaya": "63",
    "nizhnii novgorod": "52",
}
_REGION_SUBSTR: tuple[tuple[str, str], ...] = (
    ("moskv", "77"),
    ("moscow", "77"),
    ("samar", "63"),
    ("nizhnii novgorod", "52"),
    ("krasnodar", "23"),
    ("kazan", "16"),
    ("novosibirsk", "54"),
    ("ekaterinburg", "66"),
    ("ufa", "02"),
    ("chelyabinsk", "74"),
    ("krasnoyarsk", "24"),
    ("perm", "59"),
    ("voronezh", "36"),
)

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX32_RE = re.compile(r"[0-9a-fA-F]{32}")
_CARD_PATH_RE = re.compile(
    r"/vacancy/card/(?P<cc>[^/?#]+)/(?P<uuid>[0-9a-fA-F-]{36})(?:[/?#]|$)"
)
_CARD_LINK_RE = re.compile(
    r"<a[^>]*href=[\"'](?P<url>(?:https?://(?:www\.)?trudvsem\.ru)?"
    r"/vacancy/card/(?P<cc>[^\"'/?#]+)/(?P<uuid>[0-9a-fA-F-]{36}))"
    r"[\"'][^>]*>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_NEXT_LINK_RE = re.compile(
    r"<a[^>]*rel=[\"'][^\"']*next[^\"']*[\"'][^>]*>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|td|section|article)[^>]*>",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")
_TITLE_RE = re.compile(r"<h1[^>]*>(?P<t>.*?)</h1>", re.IGNORECASE | re.DOTALL)
_SALARY_RE = re.compile(
    r"<(?:span|div|p)[^>]*class=[\"'][^\"']*(?:salary|payment)[^\"']*[\"']"
    r"[^>]*>(?P<t>.*?)</(?:span|div|p)>",
    re.IGNORECASE | re.DOTALL,
)
_DESC_RE = re.compile(
    r"<(?:div|section)[^>]*class=[\"'][^\"']*(?:description|duties)"
    r"[^\"']*[\"'][^>]*>(?P<t>.*?)</(?:div|section)>",
    re.IGNORECASE | re.DOTALL,
)


class TrudvsemAPIError(RuntimeError):
    """API Trudvsem error - signal to enable HTML fallback."""


def _strip_html(value: object) -> str | None:
    if value is None:
        return None
    text = _BLOCK_TAG_RE.sub(" ", str(value))
    text = _TAG_RE.sub("", text)
    text = html_lib.unescape(text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _as_text(value: object, *, limit: int | None = None) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    return result[:limit] if limit else result


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _as_words(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        return []
    return [w.strip() for w in parts if w and w.strip()]


def _translit(value: str) -> str:
    table = {
        "Р°": "a", "Р±": "b", "РІ": "v", "Рі": "g", "Рґ": "d", "Рµ": "e",
        "С‘": "e", "Р¶": "zh", "Р·": "z", "Рё": "i", "Р№": "i", "Рє": "k",
        "Р»": "l", "Рј": "m", "РЅ": "n", "Рѕ": "o", "Рї": "p", "СЂ": "r",
        "СЃ": "s", "С‚": "t", "Сѓ": "u", "С„": "f", "С…": "kh", "С†": "ts",
        "С‡": "ch", "С€": "sh", "С‰": "shch", "СЉ": "", "С‹": "y", "СЊ": "",
        "СЌ": "e", "СЋ": "yu", "СЏ": "ya",
    }
    return "".join(table.get(ch, ch) for ch in value.lower())


def region_code_for(region: object) -> str | None:
    text = _as_text(region)
    if not text:
        return None
    key = text.strip().lower()
    digits = re.sub(r"\D", "", key)
    if 2 <= len(digits) <= 13:
        num = digits[:2]
        return (num + "0" * 13)[:13]
    if key in _REGION_NUMBERS:
        return (_REGION_NUMBERS[key] + "0" * 13)[:13]
    for substr, num in _REGION_SUBSTR:
        if substr in key or key in substr:
            return (num + "0" * 13)[:13]
    for name, num in _REGION_NUMBERS.items():
        if key in name or name in key:
            return (num + "0" * 13)[:13]
    return None


def _uuid_to_hex32(value: str) -> str | None:
    text = value.strip().lower()
    if _HEX32_RE.fullmatch(text):
        return text
    if _UUID_RE.fullmatch(text):
        return text.replace("-", "")
    return None


def _vacancy_of(item: dict) -> dict | None:
    inner = item.get("vacancy")
    if isinstance(inner, dict):
        return inner
    if isinstance(item.get("id"), str) and isinstance(
        item.get("job-name"), str
    ):
        return item
    return None


def parse_api_vacancies(payload_text: str) -> tuple[list[dict], int]:
    try:
        data = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        raise TrudvsemAPIError(f"JSON API ne razobran: {exc}") from exc
    if not isinstance(data, dict):
        raise TrudvsemAPIError("Ozhidalsya JSON-obekt API Trudvsem")
    if str(data.get("status")) not in ("200", "200 OK", "ok", "OK"):
        meta = data.get("meta")
        detail = meta.get("error") if isinstance(meta, dict) else None
        raise TrudvsemAPIError(f"API vernul status {data.get('status')}: {detail}")
    results = data.get("results")
    if not isinstance(results, dict):
        raise TrudvsemAPIError("V otvete net results")
    raw_items = results.get("vacancies")
    if not isinstance(raw_items, list):
        raise TrudvsemAPIError("V otvete net results.vacancies")
    jobs: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        vacancy = _vacancy_of(item)
        if vacancy is None:
            continue
        vid = _as_text(vacancy.get("id"))
        if not vid or vid in seen:
            continue
        seen.add(vid)
        jobs.append(vacancy)
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    try:
        total = int(str(meta.get("total", len(jobs))))
    except (TypeError, ValueError):
        total = len(jobs)
    return jobs, total


def parse_html_jobs(html_text: str) -> list[dict]:
    jobs: list[dict] = []
    seen: set[str] = set()
    for match in _CARD_LINK_RE.finditer(html_text or ""):
        uuid = match.group("uuid").lower()
        cc = match.group("cc").strip()
        hex_id = _uuid_to_hex32(uuid)
        if not hex_id or hex_id in seen:
            continue
        seen.add(hex_id)
        url = match.group("url")
        if url.startswith("/"):
            url = f"https://trudvsem.ru{url}"
        jobs.append(
            {
                "id": uuid,
                "external_id": hex_id,
                "companycode": cc,
                "url": url,
                "title": _strip_html(match.group("title")),
            }
        )
    return jobs


def has_next_page(html_text: str) -> bool:
    return bool(_NEXT_LINK_RE.search(html_text or ""))


def _fetch_html(url: str, *, timeout: float = 20.0) -> str:
    from curl_cffi import requests as curl_requests

    response = curl_requests.get(url, impersonate="chrome", timeout=timeout)
    status = int(getattr(response, "status_code", 200) or 200)
    if status != 200:
        raise TrudvsemAPIError(f"HTTP {status} ot {url}")
    return str(response.text or "")


def extract_card_fields(html_text: str) -> dict:
    text = html_text or ""
    fields: dict[str, Any] = {}

    def fill(key: str, value: Any) -> None:
        if value and not fields.get(key):
            fields[key] = value

    h1 = _TITLE_RE.search(text)
    if h1:
        fill("title", _strip_html(h1.group("t")))
    og = re.search(
        r"<meta[^>]*property=[\"']og:title[\"'][^>]*content=[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE,
    )
    if og:
        fill("title", _as_text(html_lib.unescape(og.group(1))))
    salary = _SALARY_RE.search(text)
    if salary:
        fill("salary_raw", _strip_html(salary.group("t")))
    desc = _DESC_RE.search(text)
    if desc:
        html_desc = desc.group("t").strip() or None
        fill("description_html", html_desc)
        fill("description_raw", _strip_html(html_desc))
    og_desc = re.search(
        r"<meta[^>]*property=[\"']og:description[\"'][^>]*content=[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE,
    )
    if og_desc:
        fill("description_raw", _as_text(html_lib.unescape(og_desc.group(1))))
    return fields


def _matches_employment(job: dict, employment: set[str]) -> bool:
    if not employment:
        return True
    haystack = " ".join(
        str(job.get(key) or "")
        for key in ("schedule", "employment", "employment_form", "shift")
    ).lower()
    words = set(haystack.replace(",", " ").split())
    text_h = f" {haystack} "
    if "full" in employment and (
        "full" in words or "polnyi" in words or "polnaya" in words
    ):
        return True
    if "part" in employment and (
        "part" in words
        or "partial" in words
        or "nepolnyi" in words
        or "nepolnaya" in words
    ):
        return True
    if "project" in employment and ("project" in words or "proekt" in text_h):
        return True
    if "remote" in employment and ("remote" in words or "udalen" in text_h):
        return True
    if "volunteer" in employment and "volonter" in text_h:
        return True
    if "probation" in employment and (
        "stazhirovka" in text_h or "ispytatel" in text_h or "probation" in words
    ):
        return True
    if "gph" in employment and ("gph" in words or "grazhdansko" in text_h):
        return True
    return False


def _matches_region(job: dict, region: str | None) -> bool:
    if not region:
        return True
    region_l = region.strip().lower()
    region_obj = job.get("region")
    name = ""
    code = ""
    if isinstance(region_obj, dict):
        name = str(region_obj.get("name") or "").lower()
        code = str(region_obj.get("region_code") or "")
    addr = ""
    addresses = job.get("addresses")
    if isinstance(addresses, dict):
        items = addresses.get("address")
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            addr = str(first.get("location") or "").lower()
    haystack = f"{name} {addr}"
    if region_l in haystack:
        return True
    want_code = region_code_for(region)
    if want_code and code:
        return code.strip() == want_code
    return False


def _currency_of(raw: dict) -> str | None:
    text = _as_text(raw.get("currency"), limit=16)
    if not text:
        return None
    low = text.lower().replace("В«", "").replace("В»", "").replace(".", "").strip()
    if "СЂСѓР±" in low or "rub" in low or "rur" in low:
        return "RUB"
    if "usd" in low or "РґРѕР»Р»Р°СЂ" in low:
        return "USD"
    if "eur" in low or "РµРІСЂРѕ" in low:
        return "EUR"
    return text


def _norm_experience(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        years = int(value)
        if years <= 0:
            return None
        return f"{years} Рі."
    return _as_text(value, limit=64)


def _area_of(job: dict) -> str:
    region = job.get("region")
    if isinstance(region, dict):
        name = _as_text(region.get("name"), limit=255)
        if name:
            return name
    addresses = job.get("addresses")
    if isinstance(addresses, dict):
        items = addresses.get("address")
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict):
                    loc = _as_text(entry.get("location"), limit=255)
                    if loc:
                        return loc
    direct = _as_text(job.get("area") or job.get("location"), limit=255)
    return direct or ""


def _description_of(job: dict) -> tuple[str | None, str | None]:
    duty = _strip_html(job.get("duty"))
    reqs = _strip_html(job.get("requirements"))
    skills = job.get("skills")
    skill_text = ""
    if isinstance(skills, list):
        skill_text = ", ".join(str(s).strip() for s in skills if str(s).strip())
    elif isinstance(skills, str) and skills.strip():
        skill_text = skills.strip()
    parts = [p for p in (duty, reqs) if p]
    if skill_text:
        parts.append(f"РќР°РІС‹РєРё: {skill_text}")
    raw = "\n\n".join(parts) if parts else None
    if raw is None:
        raw = _strip_html(job.get("description_raw") or job.get("description_html"))
    html_desc = _as_text(job.get("description_html"))
    if html_desc is None and raw:
        html_desc = f"<p>{html_lib.escape(raw)}</p>"
    return raw, html_desc


class TrudvsemAdapter(BaseSourceAdapter):
    """Source trudvsem: open API + HTML fallback."""

    source_name = "trudvsem"

    def __init__(self, session: AntiBanSession | None = None) -> None:
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())
        self._url_by_id: dict[str, str] = {}
        self._company_by_id: dict[str, str] = {}

    def _remember(self, external_id: str, url: str | None) -> None:
        if not external_id or not url:
            return
        self._url_by_id[external_id] = url
        match = _CARD_PATH_RE.search(url)
        if match:
            self._company_by_id[external_id] = match.group("cc")

    async def search(self, filters: dict) -> list[dict]:
        options = filters or {}
        keywords = _as_words(options.get("keywords"))
        region = _as_text(
            options.get("region") or options.get("city") or options.get("area")
        )
        employment_raw = options.get("employment_type")
        if employment_raw is None:
            employment_raw = options.get("employment_forms") or options.get(
                "employment_form"
            )
        employment = {w.lower() for w in _as_words(employment_raw)}
        limit = _as_int(options.get("max_results") or options.get("limit"))
        max_pages = max(1, int(options.get("max_pages") or 1))

        search_url = options.get("search_url")
        if search_url:
            base_url = self.validate_search_url(str(search_url))
        else:
            # Region idet cherez /region/{code} tolko dlya cifrovyh kodov.
            # Nazvaniya filtruyutsya klientskim filtrom, chtoby API-mok
            # po bazovomu URL ne lomalsya.
            digits = "".join(ch for ch in (region or "") if ch.isdigit())
            region_code = None
            if digits and len(digits) >= 2:
                region_code = region_code_for(digits)
            base_url = self.build_search_url(
                keywords=keywords or None, region=region_code
            )  # type: ignore[arg-type]

        results: list[dict] = []
        seen: set[str] = set()
        fetched_any = False
        for page_index in range(max_pages):
            try:
                jobs, _total = await self._fetch_page(base_url, page_index)
                fetched_any = True
            except Exception as exc:
                logger.warning("Trudvsem page %s failed: %s", page_index + 1, exc)
                break
            if not jobs:
                break
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                vid = _as_text(job.get("id"))
                external_id = _uuid_to_hex32(vid or "")
                if not external_id or external_id in seen:
                    continue
                if region and not _matches_region(job, region):
                    continue
                if employment and not _matches_employment(job, employment):
                    continue
                seen.add(external_id)
                url = _as_text(job.get("vac_url") or job.get("url"))
                self._remember(external_id, url)
                results.append(
                    {
                        "source": self.source_name,
                        "external_id": external_id,
                        "url": url or "",
                        **job,
                    }
                )
                if limit is not None and len(results) >= limit:
                    break
            if limit is not None and len(results) >= limit:
                break
        if not fetched_any:
            fallback = await self._fetch_html_jobs(base_url)
            for job in fallback:
                external_id = str(job.get("external_id") or "")
                if not external_id or external_id in seen:
                    continue
                seen.add(external_id)
                self._remember(external_id, _as_text(job.get("url")))
                results.append(
                    {
                        "source": self.source_name,
                        "external_id": external_id,
                        "url": job.get("url") or "",
                        **job,
                    }
                )
                if limit is not None and len(results) >= limit:
                    break
        if limit is not None:
            results = results[: max(0, limit)]
        return results

    async def get_vacancy(self, external_id_or_url: str) -> dict | None:
        resolved = self._resolve(external_id_or_url)
        if not resolved:
            return None
        hex_id, companycode, uuid = resolved
        try:
            jobs, _total = await self._fetch_api_page(API_BASE, 0)
            for job in jobs:
                if str(job.get("id") or "").lower() == uuid:
                    url = _as_text(job.get("vac_url") or job.get("url"))
                    self._remember(hex_id, url)
                    return {
                        "source": self.source_name,
                        "external_id": hex_id,
                        "url": url or "",
                        **job,
                    }
        except Exception as exc:
            logger.warning("Trudvsem API down for %s: %s", hex_id, exc)
        card_url = self._url_by_id.get(hex_id)
        if not card_url and companycode:
            card_url = card_url_for(companycode, uuid)
        if not card_url:
            return None
        try:
            response = await self.http.fetch(card_url)
        except Exception as exc:
            logger.warning("Trudvsem card unreachable %s: %s", card_url, exc)
            return None
        if response.status_code in _NOT_FOUND_STATUSES:
            return None
        if response.status_code != 200:
            return None
        fields = extract_card_fields(response.text)
        if not fields.get("title"):
            return None
        return {
            "source": self.source_name,
            "external_id": hex_id,
            "url": card_url,
            **fields,
        }

    def normalize(self, raw: dict) -> dict:
        data = raw or {}
        external_id = _as_text(data.get("external_id"))
        if not external_id:
            vid = _as_text(data.get("id"))
            external_id = _uuid_to_hex32(vid or "") if vid else None
        if not external_id:
            raise ValueError("Vacancy missing external_id")
        external_id = external_id[:32]
        url = _as_text(data.get("url") or data.get("vac_url"))
        if url:
            self._remember(external_id, url)
        else:
            url = self._url_by_id.get(external_id) or ""
        company = data.get("company")
        company_name = _as_text(data.get("company_name"), limit=512)
        if not company_name and isinstance(company, dict):
            company_name = _as_text(company.get("name"), limit=512)
        salary_from = _as_int(data.get("salary_from") or data.get("salary_min"))
        salary_to = _as_int(data.get("salary_to") or data.get("salary_max"))
        currency = _currency_of(data)
        if not currency and (salary_from or salary_to):
            currency = "RUB"
        requirement = data.get("requirement")
        experience = None
        if isinstance(requirement, dict):
            experience = _norm_experience(requirement.get("experience"))
        if experience is None:
            experience = _norm_experience(data.get("experience"))
        description_raw, description_html = _description_of(data)
        return {
            "source": self.source_name,
            "external_id": external_id,
            "url": url,
            "title": _as_text(data.get("title") or data.get("job-name"), limit=512),
            "company_name": company_name,
            "salary_from": salary_from,
            "salary_to": salary_to,
            "salary_currency": currency,
            "experience": experience,
            "employment_form": _as_text(data.get("employment_form"), limit=64),
            "work_format": _as_text(data.get("work_format"), limit=64),
            "schedule": _as_text(data.get("schedule"), limit=128),
            "area": _area_of(data),
            "published_at": _as_text(
                data.get("published_at")
                or data.get("creation-date")
                or data.get("date_modify")
            ),
            "description_raw": description_raw,
            "description_html": description_html,
        }

    def build_search_url(
        self,
        *,
        keywords: list[str] | None = None,
        region: str | None = None,
        page: int = 0,
        **_kwargs: object,
    ) -> str:
        text = None
        if keywords:
            words = [str(w).strip() for w in keywords if str(w).strip()]
            text = " ".join(words) if words else None
        region_val = region
        if region_val is None:
            city = _kwargs.get("city") or _kwargs.get("area")
            region_val = _as_text(city)
        region_code = region_code_for(region_val) if region_val else None
        offset = max(0, int(page or 0)) * PAGE_SIZE
        return api_url_for(
            text=text, region_code=region_code, offset=offset, limit=PAGE_SIZE
        )

    def validate_search_url(self, search_url: str) -> str:
        candidate = str(search_url).strip()
        parts = urlsplit(candidate)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"Search URL dolzhen byt na trudvsem.ru: {candidate!r}"
            )
        return candidate

    def page_url(self, base_url: str, page: int) -> str:
        index = max(0, int(page or 0))
        parts = urlsplit(base_url)
        if "opendata.trudvsem.ru" in (parts.hostname or ""):
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["offset"] = str(index * PAGE_SIZE)
            query["limit"] = str(PAGE_SIZE)
            new_query = "&".join(f"{k}={v}" for k, v in query.items())
            return urlunsplit(
                (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
            )
        joiner = "&" if "?" in base_url else "?"
        if index == 0:
            return base_url
        return f"{base_url}{joiner}page={index + 1}"

    def parse_listing(self, html: str) -> tuple[list[str], bool]:
        try:
            jobs, _total = parse_api_vacancies(html)
            ids: list[str] = []
            for job in jobs:
                hex_id = _uuid_to_hex32(str(job.get("id") or ""))
                if hex_id:
                    ids.append(hex_id)
                    self._remember(hex_id, _as_text(job.get("vac_url")))
            return ids, False
        except (TrudvsemAPIError, ValueError):
            pass
        jobs = parse_html_jobs(html)
        for job in jobs:
            self._remember(
                str(job.get("external_id") or ""), _as_text(job.get("url"))
            )
        return [str(j["external_id"]) for j in jobs], has_next_page(html)

    def build_vacancy_url(self, base_url: str, external_id: str) -> str:
        key = str(external_id).strip().lower()
        cached = self._url_by_id.get(key)
        if cached:
            return cached
        companycode = self._company_by_id.get(key)
        uuid = _hex32_to_uuid(key)
        if companycode and uuid:
            return card_url_for(companycode, uuid)
        if uuid:
            match = _CARD_PATH_RE.search(base_url or "")
            if match:
                return card_url_for(match.group("cc"), uuid)
        return base_url

    def extract_fields(self, html: str) -> dict:
        return extract_card_fields(html)

    async def _fetch_page(
        self, base_url: str, page_index: int
    ) -> tuple[list[dict], int]:
        if "opendata.trudvsem.ru" in base_url:
            url = self.page_url(base_url, page_index)
            return await self._fetch_api_page(url, page_index)
        url = self.page_url(base_url, page_index)
        response = await self.http.fetch(url)
        if response.status_code == 200:
            return parse_html_jobs(response.text), 0
        if response.status_code in _NOT_FOUND_STATUSES:
            return [], 0
        raise TrudvsemAPIError(f"HTML: {response.status_code}: {url}")

    async def _fetch_api_page(
        self, url: str, _page_index: int
    ) -> tuple[list[dict], int]:
        response = await self.http.fetch(url)
        if response.status_code != 200:
            raise TrudvsemAPIError(f"HTTP {response.status_code}: {url}")
        return parse_api_vacancies(response.text)

    async def _fetch_html_jobs(self, base_url: str) -> list[dict]:
        listing = LISTING_URL if "opendata.trudvsem.ru" in base_url else base_url
        try:
            html_text = await asyncio.to_thread(_fetch_html, listing)
        except Exception as exc:
            logger.warning("curl_cffi failed %s (%s)", listing, exc)
            response = await self.http.fetch(listing)
            if response.status_code in _NOT_FOUND_STATUSES:
                return []
            if response.status_code != 200:
                raise TrudvsemAPIError(f"HTML fallback: {response.status_code}") from exc
            html_text = response.text
        jobs = parse_html_jobs(html_text)
        if not jobs:
            logger.warning("Trudvsem HTML fallback: no cards")
        return jobs

    def _resolve(self, value: str) -> tuple[str, str | None, str] | None:
        text = (value or "").strip()
        if not text:
            return None
        if text.startswith(("http://", "https://")):
            match = _CARD_PATH_RE.search(text)
            if not match:
                return None
            uuid = match.group("uuid").lower()
            hex_id = _uuid_to_hex32(uuid)
            if not hex_id:
                return None
            return hex_id, match.group("cc"), uuid
        hex_id = _uuid_to_hex32(text)
        if not hex_id:
            return None
        uuid = _hex32_to_uuid(hex_id)
        if not uuid:
            return None
        return hex_id, self._company_by_id.get(hex_id), uuid


def _hex32_to_uuid(value: str) -> str | None:
    text = value.strip().lower()
    if _UUID_RE.fullmatch(text):
        return text
    if _HEX32_RE.fullmatch(text):
        return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"
    return None


def card_url_for(companycode: str, uuid: str) -> str:
    return f"{CARD_BASE}/{companycode.strip()}/{uuid.strip()}"


def api_url_for(
    *,
    text: str | None = None,
    region_code: str | None = None,
    offset: int = 0,
    limit: int = PAGE_SIZE,
) -> str:
    base = API_BASE
    if region_code:
        base = f"{API_BASE}/region/{region_code}"
    params = [f"offset={max(0, int(offset))}", f"limit={max(1, int(limit))}"]
    if text:
        params.append(f"text={quote_plus(text)}")
    return f"{base}?{'&'.join(params)}"


def listing_url_for(*, text: str | None = None) -> str:
    if text:
        return f"{LISTING_URL}?text={quote_plus(text)}"
    return LISTING_URL


def _disabled_tail() -> None:
    return None

    def _resolve(self, value: str) -> tuple[str, str | None, str] | None:
        text = (value or "").strip()
        if not text:
            return None
        if text.startswith(("http://", "https://")):
            match = _CARD_PATH_RE.search(text)
            if not match:
                return None
            uuid = match.group("uuid").lower()
            hex_id = _uuid_to_hex32(uuid)
            if not hex_id:
                return None
            return hex_id, match.group("cc"), uuid
        hex_id = _uuid_to_hex32(text)
        if not hex_id:
            return None
        uuid = _hex32_to_uuid(hex_id)
        if not uuid:
            return None
        return hex_id, self._company_by_id.get(hex_id), uuid
