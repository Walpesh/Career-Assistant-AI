# Autosearch Verification Report — Parameter Fidelity & Data Integrity Audit

**Module:** Автопоиск (Autosearch) — `POST /api/v1/parsing/auto`
**Repository:** https://github.com/Walpesh/Career-Assistant-AI
**Date:** 2026-10-11
**Test file:** `backend/tests/test_autosearch_scenarios.py`
**Result:** ✅ **20 / 20 passed** (`pytest tests/test_autosearch_scenarios.py -v`, 6.35s)
**Regression check:** ✅ `test_parsing_flow.py` + `test_multi_source.py` → **102 passed**

---

## 1. Objective & Method

Rigorous scenario-based functional review of the Autosearch module enforcing **strict
1:1 parameter fidelity** across the whole chain, with **no silent fallbacks or data
corruption**:

```
UI form input → POST /parsing/auto (Pydantic) → tasks.payload (task queue)
   → ParsingOrchestrator → SourceAdapter query params → vacancies (DB)
```

**Method note (docs-aligned):** The HTTP endpoint is an *enqueue* endpoint — it creates
a task and publishes to the ARQ/Redis queue; it does **not** synchronously hit the
network (`docs/03_API_CONTRACTS.md` §5). Therefore:

- API payload fidelity is verified **end-to-end** against `tasks.payload` in the DB.
- Adapter query-URL building and orchestrator fan-out / DB persistence are verified
  **directly** with a substituted `AntiBanSession` returning deterministic
  `FetchResponse` objects (no real requests to hh.ru / remoteok / remotive).

This is deterministic, offline, and exercises the exact production code paths
(`HHAdapter.build_search_url`, `HHAdapter._resolve_city_area`,
`ParsingOrchestrator.run_auto`, `upsert_vacancy`).

---

## 2. Scenario Results Matrix

| # | Scenario | Key assertions | Result |
|---|----------|----------------|--------|
| S1 | Base default autosearch (no city) | `city` omitted from payload; `sources=['hh']`; `task_type=parse_auto`; defaults threshold=70/max_pages=5; URL has **no** `area` | ✅ Pass (2 tests) |
| S2 | City-specific fidelity | `Новокузнецк→1240`, `Санкт-Петербург→2`, `Москва→1`; case/whitespace normalized; unknown city → `None`+warning, **no** `area`, no Moscow fallback; city preserved 1:1 in payload | ✅ Pass (10 tests) |
| S3 | Multi-source concurrent | All 3 adapters invoked (fan-out); per-source DB rows; composite uniqueness `(user_id, source, external_id)` (2nd run → 0 created, N updated) | ✅ Pass (3 tests) |
| S4 | Advanced filters & thresholds | `employment/work_format/schedule/threshold/max_pages` 1:1 into payload **and** into URL query (`employment`, `work_format`, `schedule`); multi-value comma-join; Russian UI labels rejected (400) | ✅ Pass (3 tests) |
| S5 | Blacklist filtering | `blacklist_enabled`+normalized words into payload; `is_blacklisted` matches on title/company/description; blacklisted vacancy **not persisted** | ✅ Pass (3 tests) |

**Total: 20 passed / 0 failed.**

---

## 3. Parameter Fidelity Check (UI → payload → Adapter URL)

Live-captured URLs from the production `HHAdapter`/`build_auto_search_url`:

| Scenario | UI input | `task.payload` (key) | Outgoing Adapter query URL |
|----------|----------|----------------------|----------------------------|
| S1 | keywords=`["python"]`, city=`""`, sources=`["hh"]` | `keywords=["python"]`, `sources=["hh"]`, `city` **omitted**, `match_threshold=70`, `max_pages=5` | `https://hh.ru/search/vacancy?text=python&page=0` *(no `area`)* |
| S2 | keywords=`["fastapi"]`, city=`"Новокузнецк"` | `city="Новокузнецк"` | `https://hh.ru/search/vacancy?text=fastapi&area=1240&page=0` |
| S2 | city=`"Санкт-Петербург"` | `city="Санкт-Петербург"` | `...&area=2&page=0` |
| S2 (neg) | city=`"Атлантида"` (unknown) | `city="Атлантида"` | `...text=x&page=0` — **no `area`**, warning logged |
| S3 | keywords=`["backend"]`, sources=`["hh","remoteok","remotive"]`, city=`"Москва"` | `sources` preserved order 1:1 | Each adapter gets its own search URL (`/search` per source) |
| S4 | employment=`full`, work=`remote`, schedule=`fullDay`, threshold=80, pages=2 | all preserved in payload | `...text=python&employment=full&work_format=remote&schedule=fullDay&page=0` |
| S4 (multi) | employment=`["full","part"]` | `["full","part"]` | `...&employment=full%2Cpart&page=0` (comma-join, `docs/04 §4.1`) |
| S5 | blacklist on, words=`["ГПХ","ТК РФ"]` | `blacklist_enabled=true`, `blacklist_words=["ГПХ","ТК РФ"]` | Filter applied pre-insert (not a URL param) |


---

## 4. Data Integrity Verification

- **`source` identifier:** Every vacancy is stored with the adapter's `source`
  (`hh`/`remoteok`/`remotive`), enforced by DB CHECK `ck_vacancies_source`
  (`docs/02 §3.3`). Confirmed `by_source == {hh:[h1,h2], remoteok:[r1,r2], remotive:[m1]}`.
- **Composite uniqueness:** `uq_vacancies_user_source_external` on
  `(user_id, source, external_id)`. Re-running the same autosearch yields
  `created==0, updated==3` with no duplicate rows (`external_id` is a persisted
  generated column mirroring `hh_vacancy_id` for hh).
- **`input_mode = auto`:** The task is created as `task_type="parse_auto"`
  (the autosearch input mode), stored in `tasks.task_type`/`tasks.payload`.
- **Blacklist pre-insert:** `is_blacklisted()` runs on
  `title + company_name + description_raw` **before** `upsert_vacancy`; matching
  rows are deleted/discarded (`docs/04 §4.9`). Confirmed blacklisted `bl1` absent
  while clean `ok1` persisted.

---

## 5. Identified Discrepancies / Edge-Cases

**No functional bugs were found in the backend autosearch path.** The audit
documents the following *contract subtleties* (already encoded as regression tests
to prevent silent regressions):

1. **Filter enum contract (S4).** The API expects **enum codes** (`full`, `part`,
   `project`, `remote`, `hybrid`, `onsite`, `office`, `fullDay`, `flexible`, `shift`,
   …), **not** the Russian UI labels («Полная», «Удалённо», «Полный день»). Sending a
   Russian label correctly returns **400 `VALIDATION_ERROR`** (Pydantic `Literal`)
   rather than silently dropping the filter. → *Requirement for the frontend: the UI
   must map its Russian labels to these codes before POST.* `test_s4_rejects_russian_labels`
   locks this in.
2. **Empty/unknown city is not an error (S1/S2).** Empty city → payload omits `city`
   (`model_dump(exclude_none=True)`) → no `area` (all-Russia). Unknown city →
   `_resolve_city_area` returns `None`, logs `"City '<x>' not recognized in area map;
   proceeding without area filter"`, and builds the URL **without** `area` — i.e. it
   does **not** silently fall back to Moscow (area=1). This is the documented,
   intentional fail-open behaviour (`docs/04 §4.1`).
3. **City normalization (S2).** `resolve_city_area` lowercases, strips whitespace,
   folds `ё→е` and hyphens, so `"  новокузнецк  "` resolves identically to
   `"Новокузнецк"` → `1240`.

> The negative-check requirement ("unknown city → warning + safe fallback instead of
> silently searching Moscow") is **satisfied**: the fallback is *no area filter*
> (all-Russia) plus an explicit WARNING log, which is the safe, transparent behaviour
> specified in `docs/04_PARSING_RULES.md §4.1`.

---

## 6. Test Coverage Summary

`backend/tests/test_autosearch_scenarios.py` — 20 tests:

- **S1:** `test_s1_api_default_payload_no_city`, `test_s1_empty_city_builds_url_without_area`
- **S2:** `test_s2_city_resolves_to_area` (4 params), `test_s2_area_present_in_search_url`
  (3 params), `test_s2_unknown_city_no_moscow_fallback`, `test_s2_api_preserves_city_in_payload`
- **S3:** `test_s3_api_accepts_all_selected_sources`,
  `test_s3_orchestrator_fans_out_and_persists_per_source`,
  `test_s3_composite_uniqueness_user_source_external`
- **S4:** `test_s4_api_preserves_filters_and_thresholds`,
  `test_s4_filters_serialize_into_search_url`, `test_s4_rejects_russian_labels`
- **S5:** `test_s5_api_normalizes_blacklist_into_payload`,
  `test_s5_normalize_and_match_helpers`, `test_s5_blacklisted_vacancy_not_persisted`

Fixtures: `_OkSession` (deterministic `FetchResponse`), `_FakeAdapter` /
`_BlacklistAdapter` (`BaseSourceAdapter` hooks), `auth_headers_for`/`_make_user`
(from `tests/conftest.py`).

---

## 7. Acceptance Criteria

| Criterion | Status |
|-----------|--------|
| All 5 scenarios pass with 100% parameter accuracy | ✅ 20/20 |
| Cyrillic city resolution (`Новокузнецк`→area) builds correct region filter | ✅ `area=1240` present |
| `autosearch_verification_report.md` generated | ✅ this file |

**hh.ru query-parameter names** (`backend/app/modules/parsing/urls.py::build_auto_search_url`):
`text` (keywords), `employment` (employment_forms), `work_format` (work_formats),
`schedule` (schedules), `area` (resolved city), `page` (0-based).
