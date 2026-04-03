"""
Block 1: Data Feed
Gamma API + CLOB WebSocket (fixed)
"""

import asyncio
import aiohttp
import json
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

# --- Gamma: фильтр «живых» рынков до Fetched ... ---
# Временно 5 мин (было 30), чтобы расширить пул на 1–2 запуска; потом вернуть 30.
END_SAFETY_MARGIN = timedelta(minutes=5)
CLOB_QPS_LIMIT = 2
CLOB_WINDOW_SEC = 1.0
BOOK_REFRESH_SEC = 5.0
_clob_rate_deque: deque = deque()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        s = str(value).strip()
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s + "T23:59:59+00:00")
    except Exception:
        return None


# =============================================================================
# LIVE MARKETS (зелёная зона): из Gamma-рынков строятся отфильтрованные списки
# по категориям. Лог: "Gamma filter live markets X from Y raw".
# =============================================================================
def filter_live_markets(
    raw_list: List[dict],
    now: Optional[datetime] = None,
    log: Optional[logging.Logger] = None,
) -> List[dict]:
    """После Gamma API: отсечь closed/archived, конец экспирации < safety margin, мягкие пороги по ликвидности/объёму."""
    if now is None:
        now = datetime.now(timezone.utc)
    res = []
    for m in raw_list:
        try:
            if not m.get("active", True):
                continue
            if m.get("closed", False):
                continue
            if m.get("archived", False):
                continue
            end_dt = _parse_dt(m.get("endDateIso") or m.get("endDate"))
            if end_dt and (end_dt - now) < END_SAFETY_MARGIN:
                continue
            # Временно отключено на 1–2 запуска: шире пул, чтобы найти живой ордербук
            if False:
                liq = m.get("liquidityNum")
                if liq is not None:
                    try:
                        if float(liq) < 1000:
                            continue
                    except (TypeError, ValueError):
                        pass
                vol24 = m.get("volume24hr") or m.get("volumeNum")
                if vol24 is not None:
                    try:
                        if float(vol24) < 1000:
                            continue
                    except (TypeError, ValueError):
                        pass
            res.append(m)
        except Exception as e:
            if log:
                log.warning("Gamma filter skip market %s err=%s", m.get("id"), e)
    if log:
        log.info("Gamma filter live markets %s from %s raw", len(res), len(raw_list))
    return res


async def _clob_rate_limit() -> None:
    """Ждать, пока не станет можно сделать ещё один CLOB-запрос (QPS)."""
    now = time.time()
    while _clob_rate_deque and now - _clob_rate_deque[0] > CLOB_WINDOW_SEC:
        _clob_rate_deque.popleft()
    while len(_clob_rate_deque) >= CLOB_QPS_LIMIT:
        await asyncio.sleep(0.1)
        now = time.time()
        while _clob_rate_deque and now - _clob_rate_deque[0] > CLOB_WINDOW_SEC:
            _clob_rate_deque.popleft()
    _clob_rate_deque.append(time.time())
logging.getLogger(__name__).setLevel(logging.INFO)

CLOB_BOOK_URL = "https://clob.polymarket.com/book"


def _retry_after_seconds(resp_headers, attempt: int = 0, default_sec: float = 1.0) -> float:
    """429: приоритет Retry-After (секунды или HTTP-date). Если нет — экспоненциальный backoff (без «сразу ещё раз»)."""
    ra = resp_headers.get("Retry-After") if resp_headers else None
    if ra is not None:
        try:
            return max(1.0, float(int(str(ra).strip())))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(str(ra).strip())
                delta = dt.timestamp() - time.time()
                return max(1.0, min(delta, 60.0))
            except Exception:
                pass
    return max(1.0, default_sec * (2.0 ** attempt))


# Кеш токенов без книги: не дергать CLOB повторно до истечения TTL (404/400 → "No orderbook exists")
_no_orderbook_token_ids: Dict[str, float] = {}  # token_id -> expiry (time.time())
NO_ORDERBOOK_CACHE_TTL_SEC = 1800  # 30 min

# Sample log: раз в N минут (timestamp-gate), чтобы видеть оба сценария в динамике
_clob_sample_interval_sec = 60.0
_last_clob_sample_time = 0.0
_logged_200_ok = False  # одна строка "ok" для status==200 (keys или превью JSON)
_logged_first_clob_response = False  # одна строка для первого ответа (любой статус/исключение)
_logged_raw_clob_example = False     # один пример сырого clobTokenIds из Gamma
_logged_gamma_sample_keys = False    # один раз: топ-левел ключи сырого market dict из Gamma


def _iter_levels(levels):
    """Yield (price, size) from CLOB levels (dict or [price,size])."""
    if not levels:
        return
    for lvl in levels:
        try:
            if isinstance(lvl, dict):
                p = lvl.get("price")
                s = lvl.get("size")
                if p is None:
                    continue
                pf = float(p)
                sf = float(s) if s is not None else None
                yield pf, sf
            elif isinstance(lvl, (tuple, list)) and len(lvl) >= 1:
                pf = float(lvl[0])
                sf = float(lvl[1]) if len(lvl) >= 2 else None
                yield pf, sf
        except (TypeError, ValueError):
            continue


def _best_bid(levels) -> tuple[Optional[float], Optional[float]]:
    """Return (best_bid_price, best_bid_size). Robust to unsorted levels."""
    best_p = None
    best_s = None
    for p, s in _iter_levels(levels):
        if best_p is None or p > best_p:
            best_p = p
            best_s = s
    return best_p, best_s


def _best_ask(levels) -> tuple[Optional[float], Optional[float]]:
    """Return (best_ask_price, best_ask_size). Robust to unsorted levels."""
    best_p = None
    best_s = None
    for p, s in _iter_levels(levels):
        if best_p is None or p < best_p:
            best_p = p
            best_s = s
    return best_p, best_s


def _best_size(levels) -> Optional[float]:
    """Размер на лучшем уровне: list[{\"size\": \"...\"}] или list[[price, size]]."""
    if not levels:
        return None
    top = levels[0]
    if isinstance(top, dict):
        s = top.get("size")
        if s is None:
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    if isinstance(top, (tuple, list)) and len(top) >= 2:
        try:
            return float(top[1])
        except (TypeError, ValueError):
            return None
    return None


class _ClobBookClient:
    """Тонкий клиент: get_orderbook(token_id) -> {bids, asks} через REST."""

    def __init__(self, session: aiohttp.ClientSession, url: str = CLOB_BOOK_URL):
        self._session = session
        self._url = url

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """GET /book?token_id=... Возвращает None при ошибке (нет стакана). 200: только если есть bids и asks (массивы).

        - 200 + valid JSON с bids/asks (массивы, могут быть пустыми) → возвращаем data.
        - 400/404 или тело {"error": ...} → None (книги нет), токен кешируется в no_orderbook до TTL.
        - 429 → None + warning; один ретрай с паузой Retry-After или exp backoff (без немедленного повтора).
        - остальное → None + warning.
        """
        global _no_orderbook_token_ids, _logged_first_clob_response, _logged_200_ok
        now = time.time()
        if token_id in _no_orderbook_token_ids and now < _no_orderbook_token_ids[token_id]:
            return None
        u = f"{self._url}?token_id={token_id}"
        token_id_preview = (token_id[:8] + ".." + token_id[-6:]) if len(token_id) > 16 else (token_id or "")
        for attempt in range(3):
            try:
                logger.info("CLOB book REQUEST token_id=%s", token_id)
                timeout = aiohttp.ClientTimeout(total=15)
                async with self._session.get(u, timeout=timeout, headers={"Accept": "application/json"}) as resp:
                    body_bytes = await resp.read()
                    req_h = getattr(getattr(resp, "request", None), "headers", None)
                    auth_header_present = bool(req_h and "Authorization" in req_h)
                    url_safe = f"{self._url}?token_id={token_id_preview}"
                    if resp.status != 200:
                        logger.info(
                            "CLOB book token=%s status=%d bids=- asks=-",
                            token_id_preview, resp.status,
                        )
                        if not _logged_first_clob_response:
                            logger.info(
                                "CLOB book first response: status=%d url=%s method=GET auth_header=%s token_id_preview=%s body_preview=%s",
                                resp.status, url_safe, auth_header_present, token_id_preview,
                                body_bytes[:200].decode("utf-8", errors="replace"),
                            )
                            _logged_first_clob_response = True
                        if resp.status in (404, 400):
                            _no_orderbook_token_ids[token_id] = now + NO_ORDERBOOK_CACHE_TTL_SEC
                            logger.debug(
                                "CLOB book: status=%d no orderbook for token (normal) token_id_preview=%s cached %.0fmin",
                                resp.status, token_id_preview, NO_ORDERBOOK_CACHE_TTL_SEC / 60,
                            )
                            return None
                        if resp.status == 429:
                            delay = _retry_after_seconds(resp.headers, attempt=attempt, default_sec=1.0)
                            logger.warning("CLOB book: status=429 rate-limit token_id_preview=%s delay=%.1fs attempt=%d", token_id_preview, delay, attempt + 1)
                            if attempt < 2:
                                await asyncio.sleep(delay)
                                continue
                            return None
                        logger.warning(
                            "CLOB book: url=%s status=%d body_preview=%s",
                            self._url, resp.status, body_bytes[:120].decode("utf-8", errors="replace"),
                        )
                        return None
                    try:
                        body = json.loads(body_bytes)
                    except Exception as decode_err:
                        if not _logged_first_clob_response:
                            logger.info(
                                "CLOB book first response: status=%d url=%s method=GET auth_header=%s token_id_preview=%s decode_err=%s body_preview=%s",
                                resp.status, url_safe, auth_header_present, token_id_preview, decode_err,
                                body_bytes[:200].decode("utf-8", errors="replace"),
                            )
                            _logged_first_clob_response = True
                        logger.warning(
                            "CLOB book: url=%s status=%d decode_err=%s body_preview=%s",
                            self._url, resp.status, decode_err,
                            body_bytes[:120].decode("utf-8", errors="replace"),
                        )
                        return None
                    if isinstance(body, dict) and "error" in body:
                        logger.info("CLOB book token=%s status=200 bids=- asks=- (body.error)", token_id_preview)
                        return None
                    if not isinstance(body, dict) or "bids" not in body or "asks" not in body:
                        logger.info("CLOB book token=%s status=200 bids=- asks=- (no bids/asks)", token_id_preview)
                        return None
                    if not isinstance(body.get("bids"), list) or not isinstance(body.get("asks"), list):
                        logger.info("CLOB book token=%s status=200 bids=- asks=- (bids/asks not list)", token_id_preview)
                        return None
                    bids_list = body.get("bids") or []
                    asks_list = body.get("asks") or []
                    logger.info(
                        "CLOB book token=%s status=200 bids=%d asks=%d",
                        token_id_preview, len(bids_list), len(asks_list),
                    )
                    if not _logged_first_clob_response:
                        _logged_first_clob_response = True
                    if not _logged_200_ok:
                        keys_str = list(body.keys()) if isinstance(body, dict) else type(body).__name__
                        preview = body_bytes[:200].decode("utf-8", errors="replace")
                        if len(body_bytes) > 200:
                            preview += "…"
                        logger.info(
                            "CLOB book ok: status=200 url=%s method=GET auth_header=%s token_id_preview=%s keys=%s body_preview=%s",
                            url_safe, auth_header_present, token_id_preview, keys_str, preview,
                        )
                        _logged_200_ok = True
                    return body
            except asyncio.CancelledError as e:
                # Treat as transient unless caller is shutting down.
                if attempt < 2:
                    delay = max(1.0, 2.0 ** attempt)
                    logger.warning("CLOB book: token_id=%s cancelled retry_in=%.1fs attempt=%d", token_id_preview, delay, attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                logger.warning("CLOB book: token_id=%s cancelled giving up", token_id_preview)
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                if not _logged_first_clob_response:
                    logger.info(
                        "CLOB book first response: exception=%s url=%s method=GET auth_header=? token_id_preview=%s",
                        e, f"{self._url}?token_id={token_id_preview}", token_id_preview,
                    )
                    _logged_first_clob_response = True
                if attempt < 2:
                    delay = max(1.0, 2.0 ** attempt)
                    logger.warning("CLOB book: url=%s network_error=%s retry_in=%.1fs attempt=%d", self._url, type(e).__name__, delay, attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                logger.warning("CLOB book: url=%s network_error=%s giving up", self._url, e)
                return None
            except Exception as e:
                if not _logged_first_clob_response:
                    logger.info(
                        "CLOB book first response: exception=%s url=%s method=GET auth_header=? token_id_preview=%s",
                        e, f"{self._url}?token_id={token_id_preview}", token_id_preview,
                    )
                    _logged_first_clob_response = True
                logger.warning("CLOB book: url=%s exception=%s", self._url, e)
                return None
        return None


def parse_clob_token_ids(raw) -> List[str]:
    """Robust parse: Gamma иногда кладёт clobTokenIds строкой (JSON-массив)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            v = json.loads(s)
            return [str(x) for x in v] if isinstance(v, list) else []
        except Exception:
            return []
    return []


# Временно False для эксперимента (доля 200 vs 404 по top-K). Вернуть True после — тогда снова только рынки с volume*Clob>0.
# Опционально: отфильтровать AMM-only по Gamma (fpmmLive, rfqEnabled, marketType, volumeClob).
USE_CLOB_VOLUME_FILTER = False  # TODO: restore True after 200/404 experiment

# На один запуск: не отбрасывать рынки по resolution/vol/liq (N>0 после Gamma filter, максимум рынков/токенов).
TEMPORARILY_DISABLE_CATEGORY_FILTERS = True


def _has_clob_volume(m: dict) -> bool:
    """Gamma: volume24hrClob / volumeClob / volume1wkClob / volume1moClob > 0 — тогда есть смысл дергать CLOB.
    При fpmmLive True и volumeClob==0 (AMM-only) все ключи будут 0 → False, CLOB-enrich не вызываем."""
    for key in ("volume24hrClob", "volumeClob", "volume1wkClob", "volume1moClob"):
        v = m.get(key)
        if v is None:
            continue
        try:
            if float(v) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _gamma_volume_counts(raw_list: list) -> tuple:
    """По сырому списку Gamma: сколько рынков с volume CLOB > 0 и AMM > 0."""
    clob_positive = 0
    amm_positive = 0
    for m in raw_list if isinstance(raw_list, list) else []:
        if not isinstance(m, dict):
            continue
        v_clob = m.get("volume1wkClob") or m.get("volume24hrClob") or m.get("volumeClob") or 0
        v_amm = m.get("volume1wkAmm") or m.get("volume24hrAmm") or m.get("volumeAmm") or 0
        try:
            if float(v_clob) > 0:
                clob_positive += 1
            if float(v_amm) > 0:
                amm_positive += 1
        except (TypeError, ValueError):
            pass
    return (clob_positive, amm_positive)


def _parse_outcomes_order(m: dict) -> List[str]:
    """Порядок исходов из Gamma (Yes/No и т.д.) для соответствия индексам clob_token_ids.
    Пробуем: outcomes, groupItemTitles, outcomeNames, groupItemTitle; список строк или JSON-строка."""
    raw = (
        m.get("outcomes")
        or m.get("groupItemTitles")
        or m.get("outcomeNames")
        or m.get("groupItemTitle")
    )
    if raw is None:
        return []
    if isinstance(raw, list):
        out = []
        for x in raw:
            if isinstance(x, dict):
                label = (x.get("title") or x.get("name") or x.get("label") or str(x)).strip()
                if label:
                    out.append(label)
            else:
                out.append(str(x).strip())
        return out if out else []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x).strip() for x in v]
        except Exception:
            pass
        return [s]
    return []


def _is_yes_outcome(name: str) -> bool:
    """Исход считается Yes, если название нормализовано к 'yes'."""
    return str(name).strip().lower() == "yes"


def _is_no_outcome(name: str) -> bool:
    """Исход считается No для бинарного маппинга."""
    return str(name).strip().lower() == "no"


def get_yes_token_id(m) -> Optional[str]:
    """Токен с outcome 'Yes' (или аналог) — только по маппингу outcomes_order ↔ clob_token_ids.
    Не используем clobTokenIds[0] без явного соответствия outcome 'Yes'."""
    arr = getattr(m, "clob_token_ids", None) if not isinstance(m, dict) else parse_clob_token_ids(m.get("clobTokenIds"))
    if not isinstance(arr, (list, tuple)):
        arr = []
    arr = list(arr)
    tid = getattr(m, "yes_token_id", None)
    if tid:
        return tid
    mp = getattr(m, "outcome_token_ids", None) or getattr(m, "token_ids_by_outcome", None)
    if isinstance(mp, dict):
        for k in ("YES", "Yes", "yes", True, "1"):
            if k in mp and mp[k]:
                return mp[k]
    if not arr:
        return None
    outcomes_order = getattr(m, "outcomes_order", None)
    if not isinstance(outcomes_order, (list, tuple)) or len(outcomes_order) != len(arr):
        # Fallback для бинарных: Polymarket обычно отдаёт [Yes, No] первым токеном — берём arr[0] как YES
        if len(arr) == 2:
            return arr[0]
        return arr[0] if len(arr) == 1 else None
    for i, name in enumerate(outcomes_order):
        if _is_yes_outcome(name):
            return arr[i]
    # Бинарный рынок: если есть только "No", второй токен считаем Yes
    if len(arr) == 2:
        for i, name in enumerate(outcomes_order):
            if _is_no_outcome(name):
                return arr[1 - i]
    return None


def log_quote_coverage(logger_instance, snaps: Sequence) -> None:
    """Один агрегатный лог после enrichment: сколько снапшотов получили bid/ask/оба."""
    total = len(snaps)
    has_bid = sum(1 for s in snaps if getattr(s, "yes_bid", None))
    has_ask = sum(1 for s in snaps if getattr(s, "yes_ask", None))
    has_both = sum(
        1 for s in snaps
        if getattr(s, "yes_bid", None) and getattr(s, "yes_ask", None)
    )
    logger_instance.info(
        "CLOB quotes: total=%d has_bid=%d has_ask=%d has_both=%d",
        total, has_bid, has_ask, has_both,
    )


def is_complementary_yes_book(
    bid: Optional[float],
    ask: Optional[float],
    yes_price_hint: Optional[float],
    *,
    eps_sum: float = 0.01,
    eps_mid: float = 0.02,
    delta_hint: float = 0.15,
) -> bool:
    """
    True -> стакан по YES считаем complementary/фиктивным и выкидываем.
    Режем только если одновременно: (1) bid+ask ≈ 1, (2) mid ≈ 0.5, (3) yes_price_hint далеко от 0.5.
    """
    if bid is None or ask is None:
        return False

    s = bid + ask
    mid = 0.5 * s

    if abs(s - 1.0) > eps_sum:
        return False
    if abs(mid - 0.5) > eps_mid:
        return False
    if yes_price_hint is None:
        return False
    if abs(yes_price_hint - 0.5) <= delta_hint:
        return False

    return True


async def enrich_yes_quotes_from_clob(
    snapshots: Sequence["MarketSnapshot"],
    clob_client: _ClobBookClient,
    *,
    max_concurrency: int = 10,
    timeout_s: float = 2.0,
    clob_state: Optional["ClobState"] = None,
) -> List["MarketSnapshot"]:
    """
    Заполняет snapshot.yes_bid / snapshot.yes_ask из CLOB.
    YES token через get_yes_token_id(s). При 404 yes_bid/yes_ask остаются None (рынок исключается).
    Если передан clob_state: обновляет его (book, has_bid, has_ask, last_book_ts), использует кеш при last_book_ts < BOOK_REFRESH_SEC, троттлинг CLOB_QPS_LIMIT.
    """
    sem = asyncio.Semaphore(max_concurrency)
    _slot_logged = [False]

    def _may_log_sample() -> bool:
        global _last_clob_sample_time
        now = time.time()
        if now - _last_clob_sample_time >= _clob_sample_interval_sec:
            _last_clob_sample_time = now
            return True
        return False

    async def one(s: "MarketSnapshot") -> "MarketSnapshot":
        if not getattr(s, "has_clob_volume", True):
            return s
        token_id = get_yes_token_id(s)
        if not token_id:
            return s
        now = time.time()
        if clob_state and token_id in clob_state.tokens:
            st = clob_state.tokens[token_id]
            if (now - st.last_book_ts) < BOOK_REFRESH_SEC and st.book:
                bids = st.book.get("bids") or []
                asks = st.book.get("asks") or []
                bid = _best_price(bids)
                ask = _best_price(asks)
                raw_best_bid, raw_best_ask = bid, ask
                best_bid_sz = _best_size(bids)
                best_ask_sz = _best_size(asks)
                bids_cnt = len(bids)
                asks_cnt = len(asks)
                yes_price_hint = getattr(s, "yes_price", None)
                # Диагностика: если Gamma yes_price "середина", а CLOB книга выглядит как 0.01/0.99,
                # почти наверняка выбран неверный token_id (не тот outcome) или перепутан порядок исходов.
                try:
                    if (
                        yes_price_hint is not None
                        and 0.10 <= float(yes_price_hint) <= 0.90
                        and raw_best_bid is not None
                        and raw_best_ask is not None
                        and float(raw_best_bid) <= 0.02
                        and float(raw_best_ask) >= 0.98
                    ):
                        logger.warning(
                            "CLOB enrich mismatch: yes_price_hint=%.3f but book is tail-ish (bid=%.3f ask=%.3f). "
                            "market_id=%s token_id=%s outcomes_order=%s clob_token_ids=%s",
                            float(yes_price_hint),
                            float(raw_best_bid),
                            float(raw_best_ask),
                            (getattr(s, "market_id", "") or "")[:32],
                            str(token_id)[:32],
                            str(getattr(s, "outcomes_order", None))[:120],
                            str(getattr(s, "clob_token_ids", None))[:120],
                        )
                except Exception:
                    pass
                if is_complementary_yes_book(raw_best_bid, raw_best_ask, yes_price_hint):
                    yes_bid = (1.0 - raw_best_ask) if raw_best_ask is not None else None
                    yes_ask = (1.0 - raw_best_bid) if raw_best_bid is not None else None
                    logger.debug(
                        "CLOB enrich: complementary book -> invert token_id=%s raw_bid=%.3f raw_ask=%.3f -> yes_bid=%.3f yes_ask=%.3f",
                        token_id, raw_best_bid or 0, raw_best_ask or 0, yes_bid or 0, yes_ask or 0,
                    )
                    return replace(
                        s, yes_bid=yes_bid, yes_ask=yes_ask,
                        best_bid_size=best_bid_sz, best_ask_size=best_ask_sz, book_bids_count=bids_cnt, book_asks_count=asks_cnt,
                    )
                logger.info(
                    "CLOB enrich: token_id=%s yes_bid=%s yes_ask=%s raw_best_bid=%s raw_best_ask=%s",
                    token_id, bid, ask, raw_best_bid, raw_best_ask,
                )
                return replace(
                    s, yes_bid=bid, yes_ask=ask,
                    best_bid_size=best_bid_sz, best_ask_size=best_ask_sz, book_bids_count=bids_cnt, book_asks_count=asks_cnt,
                )
        await _clob_rate_limit()
        try:
            ob = await asyncio.wait_for(
                clob_client.get_orderbook(token_id), timeout=timeout_s
            )
        except Exception as e:
            if not _slot_logged[0] and _may_log_sample():
                logger.warning(
                    "CLOB enrich: token_id=%s timeout_s=%.1f url=%s exception=%s",
                    token_id[:16], timeout_s, CLOB_BOOK_URL, e,
                )
                _slot_logged[0] = True
            return s
        if clob_state and token_id in clob_state.tokens:
            st = clob_state.tokens[token_id]
            st.last_book_ts = now
            st.book = ob
            if ob:
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                st.has_bid = bool(bids)
                st.has_ask = bool(asks)
            else:
                st.has_bid = False
                st.has_ask = False
        if ob is None:
            return s
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        if not _slot_logged[0] and _may_log_sample():
            logger.info(
                "CLOB sample: token_id=%s bids_count=%d asks_count=%d first_bid=%s first_ask=%s",
                token_id[:16], len(bids), len(asks),
                bids[:1] if bids else None,
                asks[:1] if asks else None,
            )
            _slot_logged[0] = True
        bid, best_bid_sz = _best_bid(bids)
        ask, best_ask_sz = _best_ask(asks)
        raw_best_bid, raw_best_ask = bid, ask
        bids_cnt = len(bids)
        asks_cnt = len(asks)
        yes_price_hint = getattr(s, "yes_price", None)
        try:
            if (
                yes_price_hint is not None
                and 0.10 <= float(yes_price_hint) <= 0.90
                and raw_best_bid is not None
                and raw_best_ask is not None
                and float(raw_best_bid) <= 0.02
                and float(raw_best_ask) >= 0.98
            ):
                logger.warning(
                    "CLOB enrich mismatch: yes_price_hint=%.3f but book is tail-ish (bid=%.3f ask=%.3f). "
                    "market_id=%s token_id=%s outcomes_order=%s clob_token_ids=%s",
                    float(yes_price_hint),
                    float(raw_best_bid),
                    float(raw_best_ask),
                    (getattr(s, "market_id", "") or "")[:32],
                    str(token_id)[:32],
                    str(getattr(s, "outcomes_order", None))[:120],
                    str(getattr(s, "clob_token_ids", None))[:120],
                )
        except Exception:
            pass
        # Если Gamma сигнализирует "живой" рынок (bestBid/bestAsk в середине), а CLOB-книга выглядит как 0.01/0.99 —
        # часто выбран НЕ тот токен или выбрана "комплементарная" книга.
        # Фикс: попробовать второй token_id в бинарном рынке и выбрать книгу, которая лучше согласуется
        # с Gamma (mid ближе к yes_price_hint ИЛИ best_bid/best_ask ближе к gamma_best_bid/ask).
        try:
            if (
                yes_price_hint is not None
                and 0.10 <= float(yes_price_hint) <= 0.90
                and raw_best_bid is not None
                and raw_best_ask is not None
                and float(raw_best_bid) <= 0.02
                and float(raw_best_ask) >= 0.98
            ):
                cids = getattr(s, "clob_token_ids", None) or []
                if isinstance(cids, (list, tuple)) and len(cids) == 2:
                    other_tid = cids[1] if str(cids[0]) == str(token_id) else cids[0]
                    if other_tid and str(other_tid) != str(token_id):
                        await _clob_rate_limit()
                        ob2 = await asyncio.wait_for(
                            clob_client.get_orderbook(str(other_tid)), timeout=timeout_s
                        )
                        if ob2:
                            b2, b2_sz = _best_bid(ob2.get("bids") or [])
                            a2, a2_sz = _best_ask(ob2.get("asks") or [])
                            if b2 is not None and a2 is not None and a2 >= b2:
                                mid1 = 0.5 * (float(raw_best_bid) + float(raw_best_ask))
                                mid2 = 0.5 * (float(b2) + float(a2))
                                y = float(yes_price_hint)
                                gbb = getattr(s, "gamma_best_bid", None)
                                gba = getattr(s, "gamma_best_ask", None)
                                gamma_ok = False
                                try:
                                    gamma_ok = (gbb is not None and gba is not None and float(gbb) >= 0.05 and float(gba) <= 0.95)
                                except (TypeError, ValueError):
                                    gamma_ok = False
                                # Score by closeness to Gamma yes_price + (optionally) Gamma bestBid/bestAsk
                                score1 = abs(mid1 - y)
                                score2 = abs(mid2 - y)
                                if gamma_ok:
                                    try:
                                        score1 += abs(float(raw_best_bid) - float(gbb)) + abs(float(raw_best_ask) - float(gba))
                                        score2 += abs(float(b2) - float(gbb)) + abs(float(a2) - float(gba))
                                    except (TypeError, ValueError):
                                        pass
                                if score2 + 1e-9 < score1:
                                    logger.warning(
                                        "CLOB token swap: chose other token_id (mid closer to yes_price_hint). "
                                        "market_id=%s yes_price_hint=%.3f token_id=%s mid=%.3f -> other_token_id=%s mid=%.3f",
                                        (getattr(s, "market_id", "") or "")[:32],
                                        y,
                                        str(token_id)[:32],
                                        mid1,
                                        str(other_tid)[:32],
                                        mid2,
                                    )
                                    # Обновим clob_state для other_tid, чтобы has_live_orderbook работал корректно
                                    if clob_state and str(other_tid) in clob_state.tokens:
                                        st2 = clob_state.tokens[str(other_tid)]
                                        st2.last_book_ts = now
                                        st2.book = ob2
                                        st2.has_bid = bool(ob2.get("bids") or [])
                                        st2.has_ask = bool(ob2.get("asks") or [])
                                    bid, ask = b2, a2
                                    raw_best_bid, raw_best_ask = bid, ask
                                    best_bid_sz = b2_sz
                                    best_ask_sz = a2_sz
                                    bids_cnt = len(ob2.get("bids") or [])
                                    asks_cnt = len(ob2.get("asks") or [])
                                    token_id = str(other_tid)
        except Exception:
            pass
        if is_complementary_yes_book(raw_best_bid, raw_best_ask, yes_price_hint):
            yes_bid = (1.0 - raw_best_ask) if raw_best_ask is not None else None
            yes_ask = (1.0 - raw_best_bid) if raw_best_bid is not None else None
            logger.debug(
                "CLOB enrich: complementary book -> invert token_id=%s raw_bid=%.3f raw_ask=%.3f -> yes_bid=%.3f yes_ask=%.3f",
                token_id[:16], raw_best_bid or 0, raw_best_ask or 0, yes_bid or 0, yes_ask or 0,
            )
            return replace(
                s, yes_token_id=str(token_id), yes_bid=yes_bid, yes_ask=yes_ask,
                best_bid_size=best_bid_sz, best_ask_size=best_ask_sz, book_bids_count=bids_cnt, book_asks_count=asks_cnt,
            )
        logger.info(
            "CLOB enrich: token_id=%s yes_bid=%s yes_ask=%s raw_best_bid=%s raw_best_ask=%s",
            token_id[:16], bid, ask, raw_best_bid, raw_best_ask,
        )
        return replace(
            s, yes_token_id=str(token_id), yes_bid=bid, yes_ask=ask,
            best_bid_size=best_bid_sz, best_ask_size=best_ask_sz, book_bids_count=bids_cnt, book_asks_count=asks_cnt,
        )

    return list(await asyncio.gather(*(one(s) for s in snapshots)))


@dataclass
class MarketSnapshot:
    market_id: str
    yes_price: float
    no_price: float
    spread: float
    volume_usd: float
    category: str
    question: str = ""
    group_item_title: str = ""  # Gamma: короткое название (например "Russia-Ukraine Ceasefire")
    # Бинарный (2 исхода) vs мультиисходный (3+). Сейчас работаем только с бинарными; мульти — ветка позже.
    outcomes_count: int = 2
    # Для crypto/мелких рынков: лимит размера заявки от объёма и глубины
    end_date_iso: Optional[str] = None
    seconds_to_resolution: Optional[float] = None
    liquidity_usd: float = 0.0
    # Gamma (предварительный CLOB bestBid/bestAsk, если доступно): используем для дешёвого анти-tail prefilter
    gamma_best_bid: Optional[float] = None
    gamma_best_ask: Optional[float] = None
    # v2.0 WebSocket: [yes_token_id, no_token_id] из Gamma clobTokenIds (parse_clob_token_ids)
    clob_token_ids: Optional[List[str]] = None
    # YES token id actually used for enrichment/pricing (after swap heuristic)
    yes_token_id: Optional[str] = None
    # Порядок исходов из Gamma (Yes/No), соответствует индексам clob_token_ids
    outcomes_order: Optional[List[str]] = None
    # volume1wkClob/volume24hrClob/volume1moClob > 0 — снижаем нагрузку: enrichment только для таких
    has_clob_volume: bool = True
    # Опционально: из стакана CLOB (для диагностики bid/ask и entry=ask)
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    # Глубина стакана на входе (чтобы не заходить в тонкие книги → EXPIRY по 0.01)
    best_bid_size: Optional[float] = None
    best_ask_size: Optional[float] = None
    book_bids_count: int = 0
    book_asks_count: int = 0


class ClobTokenState:
    """Состояние книги по одному CLOB-токену: обновляется в enrich, используется для позиций и кандидатов LLM."""
    __slots__ = ("market_id", "outcome", "last_book_ts", "has_bid", "has_ask", "book")

    def __init__(self, market_id: str, outcome: str = ""):
        self.market_id = market_id
        self.outcome = outcome
        self.last_book_ts = 0.0
        self.has_bid = False
        self.has_ask = False
        self.book: Optional[dict] = None


class ClobState:
    """Пул токенов и книг: token_id -> ClobTokenState, market_id -> [token_id, ...]. Пересобирается после refresh рынков."""
    def __init__(self):
        self.tokens: Dict[str, ClobTokenState] = {}
        self.market_tokens: Dict[str, List[str]] = {}

    def rebuild_from_snapshots(self, snapshots: Sequence[MarketSnapshot]) -> None:
        self.tokens.clear()
        self.market_tokens.clear()
        for s in snapshots:
            mid = getattr(s, "market_id", None) or ""
            cids = getattr(s, "clob_token_ids", None) or []
            outcomes = getattr(s, "outcomes_order", None) or []
            for idx, tid in enumerate(cids):
                if not tid:
                    continue
                outcome = outcomes[idx] if idx < len(outcomes) else f"o{idx}"
                st = ClobTokenState(mid, outcome)
                self.tokens[tid] = st
                self.market_tokens.setdefault(mid, []).append(tid)
        logger.info("CLOB state rebuilt tokens=%d markets=%d", len(self.tokens), len(self.market_tokens))


def has_live_orderbook_for_market(market_id: str, clob_state: ClobState) -> bool:
    """Есть ли у рынка хотя бы один токен с bid или ask в clob_state."""
    token_ids = clob_state.market_tokens.get(market_id)
    if not token_ids:
        return False
    for tid in token_ids:
        st = clob_state.tokens.get(tid)
        if not st:
            continue
        if st.has_bid or st.has_ask:
            return True
    return False


def is_liquid(snapshot: "MarketSnapshot", params: dict) -> bool:
    """Фильтр ликвидности для маркет-мейкинга: узкий спред, объём на лучших уровнях, глубина стакана.
    params: max_spread, min_best_level_size, min_book_levels; опционально min_24h_volume_usd."""
    bid = getattr(snapshot, "yes_bid", None)
    ask = getattr(snapshot, "yes_ask", None)
    if bid is None or ask is None:
        return False
    try:
        bid_f, ask_f = float(bid), float(ask)
    except (TypeError, ValueError):
        return False
    if ask_f <= bid_f:
        return False
    spread = ask_f - bid_f
    max_spread = params.get("max_spread")
    if max_spread is not None and spread > float(max_spread):
        return False
    min_sz = params.get("min_best_level_size")
    if min_sz is not None and float(min_sz) > 0:
        bb = getattr(snapshot, "best_bid_size", None)
        ba = getattr(snapshot, "best_ask_size", None)
        try:
            # Режем только если значение есть и меньше порога; None = нет данных, не режем
            if bb is not None and float(bb) < float(min_sz):
                return False
            if ba is not None and float(ba) < float(min_sz):
                return False
        except (TypeError, ValueError):
            return False
    min_levels = params.get("min_book_levels")
    if min_levels is not None and int(min_levels) > 0:
        nb = getattr(snapshot, "book_bids_count", 0) or 0
        na = getattr(snapshot, "book_asks_count", 0) or 0
        try:
            if int(nb) < int(min_levels) or int(na) < int(min_levels):
                return False
        except (TypeError, ValueError):
            return False
    min_vol = params.get("min_24h_volume_usd")
    if min_vol is not None:
        vol = getattr(snapshot, "volume_usd", None)
        if vol is not None:
            try:
                if float(vol) < float(min_vol):
                    return False
            except (TypeError, ValueError):
                return False
    return True


def is_liquid_diagnostic(snapshots: list, params: dict) -> dict:
    """Подсчёт по этапам: сколько отсекается на bid/ask, спреде, размере, уровнях. Для отладки liquid=0."""
    n = len(snapshots)
    has_bid_ask = 0
    spread_ok = 0
    size_ok = 0
    levels_ok = 0
    for s in snapshots:
        bid = getattr(s, "yes_bid", None)
        ask = getattr(s, "yes_ask", None)
        if bid is None or ask is None:
            continue
        has_bid_ask += 1
        try:
            bid_f, ask_f = float(bid), float(ask)
        except (TypeError, ValueError):
            continue
        if ask_f <= bid_f:
            continue
        spread = ask_f - bid_f
        max_spread = params.get("max_spread")
        if max_spread is not None and spread > float(max_spread):
            continue
        spread_ok += 1
        min_sz = params.get("min_best_level_size")
        if min_sz is not None and float(min_sz) > 0:
            bb, ba = getattr(s, "best_bid_size", None), getattr(s, "best_ask_size", None)
            if bb is not None and float(bb) < float(min_sz):
                continue
            if ba is not None and float(ba) < float(min_sz):
                continue
        size_ok += 1
        min_levels = params.get("min_book_levels")
        if min_levels is not None and int(min_levels) > 0:
            nb = getattr(s, "book_bids_count", 0) or 0
            na = getattr(s, "book_asks_count", 0) or 0
            if int(nb) < int(min_levels) or int(na) < int(min_levels):
                continue
        levels_ok += 1
    # Дополнительно: сколько имеют yes_price (Gamma) и yes_token_id — чтобы понять, почему нет bid/ask после enrich
    with_yes_price = sum(1 for s in snapshots if getattr(s, "yes_price", None) is not None)
    with_token_id = sum(1 for s in snapshots if get_yes_token_id(s) is not None)
    return {
        "total": n, "has_bid_ask": has_bid_ask, "spread_ok": spread_ok, "size_ok": size_ok, "levels_ok": levels_ok,
        "with_yes_price": with_yes_price, "with_yes_token_id": with_token_id,
    }


class ClawBotDataFeed:
    """Одна долгоживущая сессия: переиспользуется во всех fetch/enrich, закрывается только при остановке бота.
    Иначе при пачке параллельных CLOB-запросов закрытие сессии убивает коннектор и остальные падают с 'connector closed'.
    """
    def __init__(self, gamma_timeout_sec: float = 60.0, gamma_retries: int = 5):
        self.session: Optional[aiohttp.ClientSession] = None
        self._connector: Optional[aiohttp.TCPConnector] = None
        self.markets: Dict[str, MarketSnapshot] = {}
        self.gamma_base = "https://gamma-api.polymarket.com"
        self.clob_state = ClobState()
        self._shutting_down = False
        self._gamma_timeout_sec = max(10.0, float(gamma_timeout_sec))
        self._gamma_retries = max(1, int(gamma_retries))

    async def __aenter__(self):
        # Один коннектор, нигде не шарим — иначе при закрытии другой сессии с connector_owner=True закроет и его
        self._connector = aiohttp.TCPConnector(limit=40, limit_per_host=20)
        self.session = aiohttp.ClientSession(connector=self._connector)
        self._shutting_down = False
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._shutting_down = True
        if self.session:
            await self.session.close()
            self.session = None
        if self._connector:
            await self._connector.close()
            self._connector = None
        return None

    async def _reset_session(self) -> None:
        """Recreate aiohttp session/connector (Windows WinError 64, transient network drops)."""
        try:
            if self.session:
                await self.session.close()
        finally:
            self.session = None
        try:
            if self._connector:
                await self._connector.close()
        finally:
            self._connector = None
        self._connector = aiohttp.TCPConnector(limit=40, limit_per_host=20)
        self.session = aiohttp.ClientSession(connector=self._connector)

    async def _gamma_get_json(
        self,
        url: str,
        *,
        retries: Optional[int] = None,
        timeout_sec: Optional[float] = None,
    ) -> Optional[Any]:
        """GET JSON from Gamma with retries; never raises."""
        if not self.session:
            return None
        retries = self._gamma_retries if retries is None else int(retries)
        timeout_sec = self._gamma_timeout_sec if timeout_sec is None else float(timeout_sec)
        last_err: Optional[BaseException] = None
        for attempt in range(retries + 1):
            try:
                # connect отдельно: медленный DNS/SSL не съедает весь total на установку соединения
                connect_t = min(30.0, max(10.0, timeout_sec * 0.35))
                timeout = aiohttp.ClientTimeout(total=timeout_sec, connect=connect_t)
                async with self.session.get(url, timeout=timeout, headers={"Accept": "application/json"}) as resp:
                    if resp.status != 200:
                        # Retry on 5xx / 429, otherwise treat as permanent.
                        if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                            wait = _retry_after_seconds(resp.headers, attempt=attempt, default_sec=1.0)
                            logger.warning("Gamma GET %s status=%d; retry in %.1fs (attempt %d/%d)", url, resp.status, wait, attempt + 1, retries)
                            await asyncio.sleep(wait)
                            continue
                        return None
                    return await resp.json()
            except asyncio.CancelledError as e:
                # aiohttp may raise CancelledError on transient resolver/connection cancellations (Windows).
                # Do NOT swallow real shutdown cancellations.
                if self._shutting_down:
                    raise
                last_err = e
                if attempt < retries:
                    wait = max(1.0, 2.0 ** attempt)
                    logger.warning("Gamma GET cancelled; retry in %.1fs (attempt %d/%d) url=%s", wait, attempt + 1, retries, url)
                    await asyncio.sleep(wait)
                    try:
                        await self._reset_session()
                    except Exception:
                        pass
                    continue
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                last_err = e
                # Windows: WinError 64 "network name no longer available" → recreate session.
                if attempt < retries:
                    wait = max(1.0, 2.0 ** attempt)
                    logger.warning("Gamma GET failed (%s). retry in %.1fs (attempt %d/%d) url=%s", type(e).__name__, wait, attempt + 1, retries, url)
                    await asyncio.sleep(wait)
                    try:
                        await self._reset_session()
                    except Exception:
                        pass
                    continue
                break
            except Exception as e:
                last_err = e
                break
        if last_err:
            logger.warning("Gamma GET hard-failed after retries: %s url=%s", last_err, url)
        return None

    async def _gamma_get_market_by_id(self, market_id: str) -> Optional[dict]:
        """GET /markets/{id} — один рынок по числовому id (Gamma). 404 → None."""
        url = f"{self.gamma_base}/markets/{market_id}"
        data = await self._gamma_get_json(url, retries=2)
        return data if isinstance(data, dict) else None

    def _build_snapshot_from_market(self, m: dict, category_fallback: str = "crypto") -> MarketSnapshot:
        """Один снапшот из сырого объекта Gamma (тот же конструктор, что в fetch_*markets)."""
        vol = m.get("volume24h") or m.get("volume24hr") or m.get("volume") or m.get("volumeNum") or 0
        vol_f = float(vol) if vol else 0
        prices_raw = m.get("outcomePrices") or "[\"0\", \"0\"]"
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except json.JSONDecodeError:
                prices = ["0", "0"]
        else:
            prices = prices_raw
        outcomes_count = len(prices) if isinstance(prices, (list, tuple)) else 2
        yes_p = float(prices[0]) if len(prices) > 0 else 0.0
        no_p = float(prices[1]) if len(prices) > 1 else 0.0
        # Gamma (если присутствует): предварительные bestBid/bestAsk (часто отражают CLOB)
        try:
            gbb = float(m.get("bestBid")) if m.get("bestBid") is not None else None
        except (TypeError, ValueError):
            gbb = None
        try:
            gba = float(m.get("bestAsk")) if m.get("bestAsk") is not None else None
        except (TypeError, ValueError):
            gba = None
        raw_cids = m.get("clobTokenIds")
        clob_ids = parse_clob_token_ids(raw_cids)
        if len(clob_ids) < 2:
            clob_ids = None
        outcomes_order = _parse_outcomes_order(m)
        has_clob = _has_clob_volume(m) if USE_CLOB_VOLUME_FILTER else True
        cat = str(m.get("category") or category_fallback).strip().lower()
        end_d = m.get("endDate") or m.get("endDateIso") or ""
        sec_to = None
        if end_d:
            try:
                now = datetime.now(timezone.utc)
                if "T" in str(end_d):
                    end_dt = datetime.fromisoformat(str(end_d).replace("Z", "+00:00"))
                else:
                    end_dt = datetime.fromisoformat(str(end_d) + "T23:59:59+00:00")
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                sec_to = (end_dt - now).total_seconds()
            except Exception:
                pass
        liq = m.get("liquidityNum") or m.get("liquidity") or 0
        liq_f = float(liq) if liq else 0
        if liq_f < 0:
            liq_f = 0
        return MarketSnapshot(
            market_id=str(m.get("conditionId") or m.get("id") or ""),
            yes_price=yes_p,
            no_price=no_p,
            spread=abs(yes_p - no_p),
            volume_usd=vol_f,
            category=cat,
            question=str(m.get("question") or ""),
            group_item_title=str(m.get("groupItemTitle") or "").strip(),
            outcomes_count=outcomes_count,
            end_date_iso=end_d if isinstance(end_d, str) else None,
            seconds_to_resolution=sec_to if sec_to is not None else None,
            liquidity_usd=liq_f,
            gamma_best_bid=gbb,
            gamma_best_ask=gba,
            clob_token_ids=clob_ids,
            outcomes_order=outcomes_order if outcomes_order else None,
            has_clob_volume=has_clob,
        )

    async def fetch_market_by_id(self, market_id: str, category_fallback: str = "crypto") -> List[MarketSnapshot]:
        """Тестовый хелпер: один рынок по числовому id Gamma. Не используется в main_v2."""
        m = await self._gamma_get_market_by_id(market_id)
        if m is None:
            logger.warning("fetch_market_by_id: market %s not found in Gamma", market_id)
            return []
        snap = self._build_snapshot_from_market(m, category_fallback=category_fallback)
        markets = [snap]
        client = _ClobBookClient(self.session)
        to_enrich = len(markets)
        filter_hint = "volume*Clob>0" if USE_CLOB_VOLUME_FILTER else "all (filter off)"
        logger.info("CLOB enrich: fetching book for %d/%d snapshots (%s)", to_enrich, len(markets), filter_hint)
        markets = await enrich_yes_quotes_from_clob(markets, client)
        log_quote_coverage(logger, markets)
        self.markets.update({s.market_id: s for s in markets})
        return markets

    async def fetch_category_markets(
        self,
        category: str,
        min_volume_usd: float = 100_000,
        limit: int = 50,
        *,
        skip_rebuild_and_enrich: bool = False,
    ) -> List[MarketSnapshot]:
        """Gamma REST: рынки по категории (politics, sports, culture, economy).
        API /markets не поддерживает параметр category; фильтр по категории делаем через /events и теги (tag slug).
        """
        cat_lower = (category or "politics").strip().lower()
        # Загрузка через /events: у каждого события есть tags[].slug (politics, sports, economy, culture, crypto)
        url = f"{self.gamma_base}/events?active=true&closed=false&limit=200"
        data = await self._gamma_get_json(url)
        if data is None:
            logger.warning("Gamma events fetch failed for category=%s; returning 0 markets this tick", cat_lower)
            return []
        events_list = data if isinstance(data, list) else data.get("events", [])
        all_markets_raw = []
        for e in events_list:
            tags = e.get("tags") or []
            if not any((t.get("slug") or "").strip().lower() == cat_lower for t in tags):
                continue
            for m in e.get("markets") or []:
                if isinstance(m, dict):
                    all_markets_raw.append(m)
        events_with_tag = sum(
            1 for e in events_list
            if any((t.get("slug") or "").strip().lower() == cat_lower for t in (e.get("tags") or []))
        )
        logger.info(
            "Gamma events by tag: category=%s events_with_tag=%d markets_from_events=%d",
            cat_lower, events_with_tag, len(all_markets_raw),
        )
        raw_list = filter_live_markets(all_markets_raw, log=logger)  # LIVE MARKETS (зелёная зона)
        logger.info(
            "CLOB snapshot source: live_markets=%d clobTokenIds_nonempty=%d",
            len(raw_list),
            sum(1 for m in raw_list if m.get("clobTokenIds")),
        )
        global _logged_gamma_sample_keys
        if not _logged_gamma_sample_keys and raw_list and isinstance(raw_list[0], dict):
            logger.info("Gamma sample keys: keys=%s", sorted(raw_list[0].keys()))
            _logged_gamma_sample_keys = True
        clob_pos, amm_pos = _gamma_volume_counts(raw_list)
        logger.info("Gamma volume summary: clob_volume_positive=%d amm_volume_positive=%d (clob==0 -> no CLOB in sample, 404 ok)", clob_pos, amm_pos)
        markets = []
        _clob_debug_logged = 0
        for idx, m in enumerate(raw_list):  # строим снапшоты по категории из отфильтрованного live-списка
            vol = m.get("volume24h") or m.get("volume24hr") or m.get("volume") or m.get("volumeNum") or 0
            vol_f = float(vol) if vol else 0
            if not TEMPORARILY_DISABLE_CATEGORY_FILTERS and vol_f < min_volume_usd:
                continue
            prices_raw = m.get("outcomePrices") or "[\"0\", \"0\"]"
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except json.JSONDecodeError:
                    prices = ["0", "0"]
            else:
                prices = prices_raw
            outcomes_count = len(prices) if isinstance(prices, (list, tuple)) else 2
            yes_p = float(prices[0]) if len(prices) > 0 else 0.0
            no_p = float(prices[1]) if len(prices) > 1 else 0.0
            try:
                gbb = float(m.get("bestBid")) if m.get("bestBid") is not None else None
            except (TypeError, ValueError):
                gbb = None
            try:
                gba = float(m.get("bestAsk")) if m.get("bestAsk") is not None else None
            except (TypeError, ValueError):
                gba = None
            raw_cids = m.get("clobTokenIds")
            clob_ids = parse_clob_token_ids(raw_cids)
            if _clob_debug_logged < 2:
                logger.warning(
                    "CLOB snapshot debug: idx=%d clobTokenIds_type=%s len_raw=%s value_preview=%s parse_len=%d filter_cleared=%s",
                    idx,
                    type(raw_cids).__name__,
                    len(raw_cids) if isinstance(raw_cids, (list, str)) else (raw_cids and "?") or "0",
                    str(raw_cids)[:80] if raw_cids else "-",
                    len(clob_ids),
                    len(clob_ids) < 2,
                )
                _clob_debug_logged += 1
            if len(clob_ids) < 2:
                clob_ids = None
            outcomes_order = _parse_outcomes_order(m)
            global _logged_raw_clob_example
            if not _logged_raw_clob_example and raw_cids is not None:
                val_preview = str(raw_cids)[:120]
                out_preview = str(outcomes_order)[:80] if outcomes_order else str(m.get("outcomes") or m.get("groupItemTitles") or "")[:80]
                logger.info(
                    "raw clobTokenIds: type=%s value_preview=%s outcomes_preview=%s",
                    type(raw_cids).__name__, val_preview, out_preview,
                )
                _logged_raw_clob_example = True
            has_clob = _has_clob_volume(m) if USE_CLOB_VOLUME_FILTER else True
            snap = MarketSnapshot(
                market_id=str(m.get("conditionId") or m.get("id") or ""),
                yes_price=yes_p,
                no_price=no_p,
                spread=abs(yes_p - no_p),
                volume_usd=vol_f,
                category=str(m.get("category") or cat_lower),
                question=str(m.get("question") or ""),
                group_item_title=str(m.get("groupItemTitle") or "").strip(),
                outcomes_count=outcomes_count,
                gamma_best_bid=gbb,
                gamma_best_ask=gba,
                clob_token_ids=clob_ids,
                outcomes_order=outcomes_order if outcomes_order else None,
                has_clob_volume=has_clob,
            )
            markets.append(snap)
        markets_with_clob = [m for m in markets if getattr(m, "clob_token_ids", None)]
        logger.info(
            "CLOB snapshot build: markets=%d with clobTokenIds>0; sample_ids=%s",
            len(markets_with_clob),
            ", ".join(str(getattr(m, "clob_token_ids", []))[:40] for m in markets_with_clob[:3]) if markets_with_clob else "-",
        )
        if skip_rebuild_and_enrich:
            return markets
        self.clob_state.rebuild_from_snapshots(markets)
        client = _ClobBookClient(self.session)
        to_enrich = sum(1 for s in markets if getattr(s, "has_clob_volume", True))
        filter_hint = "volume*Clob>0" if USE_CLOB_VOLUME_FILTER else "all (filter off)"
        logger.info("CLOB enrich: fetching book for %d/%d %s snapshots (%s)", to_enrich, len(markets), cat_lower, filter_hint)
        markets = await enrich_yes_quotes_from_clob(markets, client, clob_state=self.clob_state)
        log_quote_coverage(logger, markets)
        self.markets.update({m.market_id: m for m in markets})
        binary_count = sum(1 for m in markets if m.outcomes_count == 2)
        logger.info("Fetched %d %s markets (%d binary; vol >= %.0f)",
                    len(markets), cat_lower, binary_count, min_volume_usd)
        return markets

    async def fetch_politics_markets(self, min_volume_usd: float = 500_000) -> List[MarketSnapshot]:
        """Gamma REST: топ рынки Politics. Обёртка над fetch_category_markets."""
        return await self.fetch_category_markets("politics", min_volume_usd=min_volume_usd)

    async def fetch_crypto_markets(
        self,
        min_volume_usd: float = 10_000,
        min_liquidity_usd: float = 1_000,
        max_hours_to_resolution: float = 1.0,
        limit: int = 50,
        *,
        skip_rebuild_and_enrich: bool = False,
    ) -> List[MarketSnapshot]:
        """Рынки Crypto с фильтром по времени до экспирации (1h), объёму и ликвидности.
        Чтобы наша заявка не двигала рынок: дальше в main размер ограничивают по объёму/глубине."""
        url = f"{self.gamma_base}/markets?active=true&closed=false&category=crypto&limit={limit}"
        data = await self._gamma_get_json(url)
        if data is None:
            logger.warning("Gamma crypto markets fetch failed; returning 0 markets this tick")
            return []
        raw_list = data if isinstance(data, list) else data.get("markets", [])
        raw_list = filter_live_markets(raw_list, log=logger)  # LIVE MARKETS (зелёная зона): лог "Gamma filter live markets X from Y raw"
        logger.info(
            "CLOB snapshot source: live_markets=%d clobTokenIds_nonempty=%d",
            len(raw_list),
            sum(1 for m in raw_list if m.get("clobTokenIds")),
        )
        global _logged_gamma_sample_keys
        if not _logged_gamma_sample_keys and raw_list and isinstance(raw_list[0], dict):
            logger.info("Gamma sample keys: keys=%s", sorted(raw_list[0].keys()))
            _logged_gamma_sample_keys = True
        clob_pos, amm_pos = _gamma_volume_counts(raw_list)
        logger.info("Gamma volume summary: clob_volume_positive=%d amm_volume_positive=%d (clob==0 -> no CLOB in sample, 404 ok)", clob_pos, amm_pos)
        now = datetime.now(timezone.utc)
        max_seconds = max_hours_to_resolution * 3600
        markets = []
        _clob_debug_logged = 0
        for idx, m in enumerate(raw_list):  # строим снапшоты crypto из отфильтрованного live-списка
            # Ранний лог для первых 2 элементов ДО любых фильтров (чтобы видеть clobTokenIds даже если все отфильтруются)
            if idx < 2:
                _raw = m.get("clobTokenIds")
                _parsed = parse_clob_token_ids(_raw)
                logger.warning(
                    "CLOB snapshot debug (crypto) early: idx=%d clobTokenIds_type=%s len_raw=%s value_preview=%s parse_len=%d",
                    idx, type(_raw).__name__,
                    len(_raw) if isinstance(_raw, (list, str)) else (_raw and "?") or "0",
                    str(_raw)[:80] if _raw else "-",
                    len(_parsed),
                )
            # Запрос уже с &category=crypto; в ответе Gamma часто не заполняет market["category"], поэтому
            # не отбрасываем по cat != "crypto" — считаем все возвращённые рынки крипто.
            cat = (m.get("category") or "").strip().lower()
            if cat and cat != "crypto":
                continue
            vol = m.get("volume24h") or m.get("volume24hr") or m.get("volume") or m.get("volumeNum") or 0
            vol_f = float(vol) if vol else 0
            if not TEMPORARILY_DISABLE_CATEGORY_FILTERS and vol_f < min_volume_usd:
                continue
            liq = m.get("liquidityNum") or m.get("liquidity") or 0
            liq_f = float(liq) if liq else 0
            if liq_f < 0:
                liq_f = 0
            if not TEMPORARILY_DISABLE_CATEGORY_FILTERS and min_liquidity_usd > 0 and liq_f < min_liquidity_usd:
                continue
            end_d = m.get("endDate") or m.get("endDateIso") or ""
            if not TEMPORARILY_DISABLE_CATEGORY_FILTERS and not end_d:
                continue
            try:
                if end_d:
                    if "T" in str(end_d):
                        end_dt = datetime.fromisoformat(str(end_d).replace("Z", "+00:00"))
                    else:
                        end_dt = datetime.fromisoformat(str(end_d) + "T23:59:59+00:00")
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    sec_to = (end_dt - now).total_seconds()
                else:
                    end_dt = None
                    sec_to = None
            except Exception:
                sec_to = None
                end_dt = None
            if not TEMPORARILY_DISABLE_CATEGORY_FILTERS and sec_to is not None and (sec_to <= 0 or sec_to > max_seconds):
                continue
            prices_raw = m.get("outcomePrices") or "[\"0\", \"0\"]"
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except json.JSONDecodeError:
                    prices = ["0", "0"]
            else:
                prices = prices_raw
            outcomes_count = len(prices) if isinstance(prices, (list, tuple)) else 2
            yes_p = float(prices[0]) if len(prices) > 0 else 0.0
            no_p = float(prices[1]) if len(prices) > 1 else 0.0
            try:
                gbb = float(m.get("bestBid")) if m.get("bestBid") is not None else None
            except (TypeError, ValueError):
                gbb = None
            try:
                gba = float(m.get("bestAsk")) if m.get("bestAsk") is not None else None
            except (TypeError, ValueError):
                gba = None
            raw_cids = m.get("clobTokenIds")
            clob_ids = parse_clob_token_ids(raw_cids)
            if _clob_debug_logged < 2:
                logger.warning(
                    "CLOB snapshot debug (crypto): idx=%d clobTokenIds_type=%s len_raw=%s value_preview=%s parse_len=%d filter_cleared=%s",
                    idx,
                    type(raw_cids).__name__,
                    len(raw_cids) if isinstance(raw_cids, (list, str)) else (raw_cids and "?") or "0",
                    str(raw_cids)[:80] if raw_cids else "-",
                    len(clob_ids),
                    len(clob_ids) < 2,
                )
                _clob_debug_logged += 1
            if len(clob_ids) < 2:
                clob_ids = None
            outcomes_order = _parse_outcomes_order(m)
            global _logged_raw_clob_example
            if not _logged_raw_clob_example and raw_cids is not None:
                val_preview = str(raw_cids)[:120]
                out_preview = str(outcomes_order)[:80] if outcomes_order else str(m.get("outcomes") or m.get("groupItemTitles") or "")[:80]
                logger.info(
                    "raw clobTokenIds: type=%s value_preview=%s outcomes_preview=%s",
                    type(raw_cids).__name__, val_preview, out_preview,
                )
                _logged_raw_clob_example = True
            has_clob = _has_clob_volume(m) if USE_CLOB_VOLUME_FILTER else True
            snap = MarketSnapshot(
                market_id=str(m.get("conditionId") or m.get("id") or ""),
                yes_price=yes_p,
                no_price=no_p,
                spread=abs(yes_p - no_p),
                volume_usd=vol_f,
                category=str(m.get("category") or "crypto"),
                question=str(m.get("question") or ""),
                group_item_title=str(m.get("groupItemTitle") or "").strip(),
                outcomes_count=outcomes_count,
                end_date_iso=end_d if isinstance(end_d, str) else None,
                seconds_to_resolution=sec_to if sec_to is not None else None,
                liquidity_usd=liq_f,
                gamma_best_bid=gbb,
                gamma_best_ask=gba,
                clob_token_ids=clob_ids,
                outcomes_order=outcomes_order if outcomes_order else None,
                has_clob_volume=has_clob,
            )
            markets.append(snap)
        markets_with_clob = [m for m in markets if getattr(m, "clob_token_ids", None)]
        if len(markets) == 0 and len(raw_list) > 0:
            m0 = raw_list[0]
            cat0 = (m0.get("category") or "").strip().lower()
            vol0 = float(m0.get("volume24h") or m0.get("volume24hr") or m0.get("volume") or m0.get("volumeNum") or 0)
            end0 = m0.get("endDate") or m0.get("endDateIso") or ""
            liq0 = float(m0.get("liquidityNum") or m0.get("liquidity") or 0)
            logger.warning(
                "CLOB crypto: 0 markets from %d raw; first item: cat=%s vol=%.0f liq=%.0f endDate=%s (check filters: category, min_volume=%s, min_liq=%s, resolution window)",
                len(raw_list), cat0, vol0, liq0, end0[:24] if end0 else "-", min_volume_usd, min_liquidity_usd,
            )
        logger.info(
            "CLOB snapshot build: markets=%d with clobTokenIds>0; sample_ids=%s",
            len(markets_with_clob),
            ", ".join(str(getattr(m, "clob_token_ids", []))[:40] for m in markets_with_clob[:3]) if markets_with_clob else "-",
        )
        if skip_rebuild_and_enrich:
            return markets
        self.clob_state.rebuild_from_snapshots(markets)
        client = _ClobBookClient(self.session)
        to_enrich = sum(1 for s in markets if getattr(s, "has_clob_volume", True))
        filter_hint = "volume*Clob>0" if USE_CLOB_VOLUME_FILTER else "all (filter off)"
        logger.info("CLOB enrich: fetching book for %d/%d crypto snapshots (%s)", to_enrich, len(markets), filter_hint)
        markets = await enrich_yes_quotes_from_clob(markets, client, clob_state=self.clob_state)
        log_quote_coverage(logger, markets)
        self.markets.update({m.market_id: m for m in markets})
        binary_count = sum(1 for m in markets if m.outcomes_count == 2)
        logger.info("Fetched %d crypto markets (%d binary; resolution <= %.1fh, vol >= %.0f, liq >= %.0f)",
                    len(markets), binary_count, max_hours_to_resolution, min_volume_usd, min_liquidity_usd)
        return markets

    async def enrich_snapshots_with_clob(self, snapshots: List[MarketSnapshot]) -> List[MarketSnapshot]:
        """Один общий enrich по списку снапшотов (после единого rebuild). Обновляет clob_state и возвращает снапшоты с yes_bid/yes_ask."""
        if not snapshots:
            return snapshots
        client = _ClobBookClient(self.session)
        enriched = await enrich_yes_quotes_from_clob(snapshots, client, clob_state=self.clob_state)
        log_quote_coverage(logger, enriched)
        return enriched

    def _binary_markets(self):
        """Только бинарные рынки (2 исхода). Мультиисходные — отдельная ветка анализа позже."""
        all_binary = []
        for m in self.markets.values():
            oc = getattr(m, "outcomes_count", None)
            if oc is None:
                cids = getattr(m, "clob_token_ids", None)
                oc = len(cids) if cids and isinstance(cids, (list, tuple)) else 2
            if oc == 2:
                all_binary.append(m)
        logger.info("DEBUG _binary_markets: %d from %d total", len(all_binary), len(self.markets))
        return all_binary

    def get_top_mispricing(self, top_n: int = 5) -> List[MarketSnapshot]:
        """Топ N бинарных рынков по спреду для стратегии (без фильтра по цене)."""
        return sorted(self._binary_markets(), key=lambda m: m.spread, reverse=True)[:top_n]

    def get_tradeable_top(
        self,
        top_n: int,
        max_entry: float = 0.99,
        min_yes: float = 0.02,
        exclude_ids: Optional[set] = None,
        liquidity_params: Optional[dict] = None,
    ) -> List[MarketSnapshot]:
        """Сначала отбор по пригодности (YES в диапазоне), потом по ликвидности стакана, топ по спреду. Только бинарные рынки.
        liquidity_params: max_spread, min_best_level_size, min_book_levels — чтобы не заходить в тонкие книги (риск EXPIRY по 0.01)."""
        logger.info("DEBUG HARDCODE CALL: min_yes=%.5f max_entry=%.4f", min_yes, max_entry)
        exclude_ids = exclude_ids or set()
        liquidity_params = liquidity_params or {}
        max_spread = liquidity_params.get("max_spread")
        min_best_level_size = liquidity_params.get("min_best_level_size")
        min_book_levels = liquidity_params.get("min_book_levels")
        binary_markets = self._binary_markets()

        rej_excluded = rej_below_min = rej_above_max = rej_invalid_price = rej_missing_snapshot = 0
        rej_missing_bid = rej_missing_ask = rej_invalid = 0
        rej_spread = rej_best_size = rej_book_levels = 0
        cnt_zero = cnt_one = cnt_mid = 0
        tradeable = []
        all_yes: List[float] = []
        yes_positive: List[float] = []
        cnt_tiny = 0
        bid_vals: List[float] = []
        ask_vals: List[float] = []
        spreads_rejected: List[float] = []  # диагностика: спреды отсечённых по max_spread

        for m in binary_markets:
            if not m or m.market_id not in self.markets:
                rej_missing_snapshot += 1
                continue
            yes = m.yes_price
            if yes is None or (isinstance(yes, float) and math.isnan(yes)):
                rej_invalid_price += 1
                continue
            if yes == 0 or yes == 0.0:
                cnt_zero += 1
            elif yes == 1 or yes == 1.0:
                cnt_one += 1
            else:
                cnt_mid += 1
            all_yes.append(yes)
            if yes > 0:
                yes_positive.append(yes)
                if yes < min_yes:
                    cnt_tiny += 1
            bid = getattr(m, "yes_bid", None)
            ask = getattr(m, "yes_ask", None)
            # Честные счётчики до continue: считаем missing_bid и missing_ask по всем рынкам
            if bid is None or (isinstance(bid, (int, float)) and bid <= 0.0):
                rej_missing_bid += 1
            if ask is None or (isinstance(ask, (int, float)) and ask <= 0.0):
                rej_missing_ask += 1
            if bid is not None and not (isinstance(bid, float) and math.isnan(bid)):
                bid_vals.append(float(bid))
            if ask is not None and not (isinstance(ask, float) and math.isnan(ask)):
                ask_vals.append(float(ask))
            if m.market_id in exclude_ids:
                rej_excluded += 1
                continue
            # Жёсткий фильтр: нет bid/ask → reject
            if bid is None or (isinstance(bid, (int, float)) and bid <= 0.0):
                continue
            if ask is None or (isinstance(ask, (int, float)) and ask <= 0.0):
                continue
            if ask < bid:
                rej_invalid += 1  # crossed book / кривые данные
                continue
            entry = ask  # покупка по аску
            if entry < min_yes:
                rej_below_min += 1
                continue
            if entry >= max_entry:
                rej_above_max += 1
                continue
            # Ликвидность стакана: не заходить в тонкие книги (риск EXPIRY по 0.01)
            if max_spread is not None and bid is not None and ask is not None:
                spread_clob = float(ask) - float(bid)
                if spread_clob > float(max_spread):
                    rej_spread += 1
                    spreads_rejected.append(spread_clob)
                    continue
            if min_best_level_size is not None and float(min_best_level_size) > 0:
                bb = getattr(m, "best_bid_size", None)
                ba = getattr(m, "best_ask_size", None)
                # Если CLOB не вернул size (оба None) — не режем по size, иначе 0 кандидатов
                if bb is None and ba is None:
                    pass
                else:
                    try:
                        min_sz = min(
                            float(bb) if bb is not None else float("inf"),
                            float(ba) if ba is not None else float("inf"),
                        )
                    except (TypeError, ValueError):
                        min_sz = 0
                    if min_sz < float(min_best_level_size):
                        rej_best_size += 1
                        continue
            if min_book_levels is not None:
                n_bid = getattr(m, "book_bids_count", 0) or 0
                n_ask = getattr(m, "book_asks_count", 0) or 0
                if int(n_bid) < int(min_book_levels) or int(n_ask) < int(min_book_levels):
                    rej_book_levels += 1
                    continue
            tradeable.append(m.market_id)

        if rej_spread or rej_best_size or rej_book_levels:
            msg = "Rejects liquidity: spread=%d best_size=%d book_levels=%d" % (rej_spread, rej_best_size, rej_book_levels)
            if spreads_rejected:
                msg += " (spread rejected: min=%.2f max=%.2f)" % (min(spreads_rejected), max(spreads_rejected))
            logger.info(msg)
        logger.info(
            "Rejects: excluded=%d below_min=%d above_max=%d invalid=%d missing=%d missing_bid=%d missing_ask=%d crossed=%d; Prices: zero=%d one=%d mid=%d",
            rej_excluded, rej_below_min, rej_above_max, rej_invalid_price, rej_missing_snapshot,
            rej_missing_bid, rej_missing_ask, rej_invalid,
            cnt_zero, cnt_one, cnt_mid,
        )
        yes_min_pos = min(yes_positive) if yes_positive else None
        yes_max = max(all_yes) if all_yes else None
        tiny_str = "Tiny: cnt_tiny=%d yes_min_pos=%s yes_max=%s" % (
            cnt_tiny,
            "%.4f" % yes_min_pos if yes_min_pos is not None else "n/a",
            "%.4f" % yes_max if yes_max is not None else "n/a",
        )
        if bid_vals or ask_vals:
            bid_min = min(bid_vals) if bid_vals else None
            bid_max = max(bid_vals) if bid_vals else None
            ask_min = min(ask_vals) if ask_vals else None
            ask_max = max(ask_vals) if ask_vals else None
            tiny_str += "; bid_min=%s bid_max=%s ask_min=%s ask_max=%s" % (
                "%.4f" % bid_min if bid_min is not None else "n/a",
                "%.4f" % bid_max if bid_max is not None else "n/a",
                "%.4f" % ask_min if ask_min is not None else "n/a",
                "%.4f" % ask_max if ask_max is not None else "n/a",
            )
        logger.info(tiny_str)

        top_tradeable = sorted(
            tradeable,
            key=lambda mid: getattr(self.markets.get(mid), "spread", 0),
            reverse=True,
        )[:top_n]
        logger.info("FINAL candidates: %d (top %d of %d)", len(top_tradeable), top_n, len(tradeable))
        return [self.markets[mid] for mid in top_tradeable if mid in self.markets]

    def tradeable_diagnostic(
        self,
        max_entry: float = 0.99,
        min_yes: float = 0.02,
        exclude_ids: Optional[set] = None,
    ) -> dict:
        """Диагностика: сколько бинарных отсекается по YES и по exclude. Главный резак — yes_price >= max_entry."""
        exclude_ids = exclude_ids or set()
        binary = self._binary_markets()
        yes_above = sum(1 for m in binary if m.yes_price >= max_entry)
        yes_below = sum(1 for m in binary if m.yes_price < min_yes)
        in_range = sum(1 for m in binary if min_yes <= m.yes_price < max_entry)
        excluded_by_id = sum(1 for m in binary if min_yes <= m.yes_price < max_entry and m.market_id in exclude_ids)
        return {
            "binary_total": len(binary),
            "yes_above_max_entry": yes_above,
            "yes_below_min": yes_below,
            "in_yes_range": in_range,
            "excluded_held_cooldown": excluded_by_id,
        }

    # TODO: ветка мультиисходных рынков (outcomes_count >= 3): отдельный отбор и логика анализа/торговли


async def test_datafeed():
    """Тест Data Feed"""
    async with ClawBotDataFeed() as feed:
        markets = await feed.fetch_politics_markets()
        top = feed.get_top_mispricing(5)
        logger.info(f"Top {len(top)} candidates")
        return top


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(test_datafeed())
