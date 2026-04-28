from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx
from sqlmodel import Session, select

from app.models import (
    MarketClaim,
    MarketContentArtifact,
    MarketDataSource,
    MarketInsightRun,
    MarketSignal,
    ScheduledPost,
    User,
)
from app.services.llm_service import GeminiConfigurationError, GeminiGenerationError, LLMService
from app.services.media_generation_service import (
    INTERNAL_IMAGE_PROVIDER,
    create_media_generation_job,
    media_generation_queue_enabled,
)

logger = logging.getLogger(__name__)

DEFAULT_FREDDIE_MAC_URL = "https://www.freddiemac.com/pmms/docs/PMMS_history.csv"
READY_STATUS = "ready"
NEEDS_REVIEW_STATUS = "needs_review"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def _json_loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _today() -> date:
    configured = os.getenv("MARKET_PIPELINE_RUN_DATE")
    if configured:
        return date.fromisoformat(configured)
    return _now().date()


def _parse_source_date(value: str) -> date:
    cleaned = value.strip()
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return date.fromisoformat(cleaned[:10])


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MarketSignalPayload:
    source_code: str
    source_name: str
    source_url: Optional[str]
    cadence: str
    attribution: str
    metric_key: str
    label: str
    value: float
    unit: str
    as_of_date: date
    geography: str = "US"
    period_start: Optional[date] = None
    period_end: Optional[date] = None
    change_value: Optional[float] = None
    change_pct: Optional[float] = None
    confidence: float = 1.0
    metadata: Optional[dict[str, Any]] = None

    @property
    def idempotency_key(self) -> str:
        return f"{self.source_code}:{self.metric_key}:{self.geography}:{self.as_of_date.isoformat()}"


class FreddieMacMortgageRateAdapter:
    code = "freddie_mac_pmms"
    name = "Freddie Mac Primary Mortgage Market Survey"
    cadence = "weekly"
    source_url = DEFAULT_FREDDIE_MAC_URL
    attribution = "Freddie Mac Primary Mortgage Market Survey"

    async def fetch(self) -> list[MarketSignalPayload]:
        if os.getenv("MARKET_PIPELINE_ENABLE_REMOTE_FETCH", "false").lower() == "true":
            try:
                return await self._fetch_remote()
            except Exception as exc:
                logger.warning("Freddie Mac PMMS fetch failed; using configured fallback: %s", exc)
        return self._fallback()

    async def _fetch_remote(self) -> list[MarketSignalPayload]:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(os.getenv("FREDDIE_MAC_PMMS_URL", self.source_url))
            response.raise_for_status()

        rows = list(csv.DictReader(io.StringIO(response.text)))
        if not rows:
            raise ValueError("Freddie Mac PMMS CSV returned no rows.")

        fieldnames = list(rows[0].keys())
        date_key = next((key for key in fieldnames if "date" in key.lower() or "week" in key.lower()), None)
        rate_key = next(
            (
                key
                for key in fieldnames
                if "30" in key.lower()
                and ("frm" in key.lower() or "fixed" in key.lower() or "yr" in key.lower())
            ),
            None,
        )
        if not date_key or not rate_key:
            raise ValueError("Freddie Mac PMMS CSV columns were not recognized.")

        dated_rows = []
        for row in rows:
            raw_date = str(row.get(date_key) or "").strip()
            raw_rate = str(row.get(rate_key) or "").strip()
            if not raw_date or not raw_rate:
                continue
            try:
                dated_rows.append((_parse_source_date(raw_date), row))
            except ValueError:
                continue
        if not dated_rows:
            raise ValueError("Freddie Mac PMMS CSV contained no parseable dated rate rows.")

        as_of, latest = max(dated_rows, key=lambda item: item[0])
        value = float(str(latest[rate_key]).strip().replace("%", ""))
        return [
            MarketSignalPayload(
                source_code=self.code,
                source_name=self.name,
                source_url=self.source_url,
                cadence=self.cadence,
                attribution=self.attribution,
                metric_key="mortgage_rate_30y_fixed",
                label="30-year fixed mortgage rate",
                value=value,
                unit="percent",
                as_of_date=as_of,
                period_start=as_of,
                period_end=as_of,
                metadata={"fetchMode": "remote", "rawColumn": rate_key},
            )
        ]

    def _fallback(self) -> list[MarketSignalPayload]:
        as_of = date.fromisoformat(os.getenv("MARKET_PIPELINE_FALLBACK_RATE_DATE", _today().isoformat()))
        value = float(os.getenv("MARKET_PIPELINE_FALLBACK_30Y_RATE", "6.78"))
        return [
            MarketSignalPayload(
                source_code=self.code,
                source_name=self.name,
                source_url=self.source_url,
                cadence=self.cadence,
                attribution=self.attribution,
                metric_key="mortgage_rate_30y_fixed",
                label="30-year fixed mortgage rate",
                value=value,
                unit="percent",
                as_of_date=as_of,
                period_start=as_of,
                period_end=as_of,
                confidence=0.65,
                metadata={"fetchMode": "configured_fallback"},
            )
        ]


class ConfiguredNationalHousingAdapter:
    code = "configured_national_housing"
    name = "Configured National Housing Snapshot"
    cadence = "monthly"
    source_url = None
    attribution = "Configured national housing market snapshot"

    async def fetch(self) -> list[MarketSignalPayload]:
        as_of = date.fromisoformat(os.getenv("MARKET_PIPELINE_HOUSING_DATE", _today().isoformat()))
        inventory = float(os.getenv("MARKET_PIPELINE_ACTIVE_INVENTORY", "1230000"))
        inventory_yoy = float(os.getenv("MARKET_PIPELINE_ACTIVE_INVENTORY_YOY", "4.2"))
        median_price = float(os.getenv("MARKET_PIPELINE_MEDIAN_PRICE", "412400"))
        median_price_yoy = float(os.getenv("MARKET_PIPELINE_MEDIAN_PRICE_YOY", "1.8"))
        return [
            MarketSignalPayload(
                source_code=self.code,
                source_name=self.name,
                source_url=self.source_url,
                cadence=self.cadence,
                attribution=self.attribution,
                metric_key="active_inventory",
                label="active housing inventory",
                value=inventory,
                unit="homes",
                as_of_date=as_of,
                period_start=as_of.replace(day=1),
                period_end=as_of,
                change_pct=inventory_yoy,
                confidence=0.7,
                metadata={"fetchMode": "configured_fallback"},
            ),
            MarketSignalPayload(
                source_code=self.code,
                source_name=self.name,
                source_url=self.source_url,
                cadence=self.cadence,
                attribution=self.attribution,
                metric_key="median_home_price",
                label="median home price",
                value=median_price,
                unit="usd",
                as_of_date=as_of,
                period_start=as_of.replace(day=1),
                period_end=as_of,
                change_pct=median_price_yoy,
                confidence=0.7,
                metadata={"fetchMode": "configured_fallback"},
            ),
        ]


def _adapters() -> list[Any]:
    return [
        FreddieMacMortgageRateAdapter(),
        ConfiguredNationalHousingAdapter(),
    ]


def _upsert_source(session: Session, payload: MarketSignalPayload) -> MarketDataSource:
    source = session.exec(
        select(MarketDataSource).where(MarketDataSource.code == payload.source_code)
    ).first()
    if source is None:
        source = MarketDataSource(
            code=payload.source_code,
            name=payload.source_name,
            sourceUrl=payload.source_url,
            cadence=payload.cadence,
            attribution=payload.attribution,
        )
    else:
        source.name = payload.source_name
        source.sourceUrl = payload.source_url
        source.cadence = payload.cadence
        source.attribution = payload.attribution
        source.active = True
        source.updatedAt = _now()
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _upsert_signal(session: Session, payload: MarketSignalPayload) -> MarketSignal:
    source = _upsert_source(session, payload)
    signal = session.exec(
        select(MarketSignal).where(MarketSignal.idempotencyKey == payload.idempotency_key)
    ).first()
    metadata = _json_dumps(payload.metadata or {})
    now = _now()
    if signal is None:
        signal = MarketSignal(
            sourceId=int(source.id),
            metricKey=payload.metric_key,
            geography=payload.geography,
            label=payload.label,
            value=payload.value,
            unit=payload.unit,
            periodStart=payload.period_start,
            periodEnd=payload.period_end,
            asOfDate=payload.as_of_date,
            changeValue=payload.change_value,
            changePct=payload.change_pct,
            confidence=payload.confidence,
            metadataJson=metadata,
            idempotencyKey=payload.idempotency_key,
        )
    else:
        signal.sourceId = int(source.id)
        signal.label = payload.label
        signal.value = payload.value
        signal.unit = payload.unit
        signal.periodStart = payload.period_start
        signal.periodEnd = payload.period_end
        signal.changeValue = payload.change_value
        signal.changePct = payload.change_pct
        signal.confidence = payload.confidence
        signal.metadataJson = metadata
        signal.updatedAt = now
    session.add(signal)
    session.commit()
    session.refresh(signal)
    return signal


async def ingest_market_signals(session: Session) -> list[MarketSignal]:
    signals: list[MarketSignal] = []
    for adapter in _adapters():
        try:
            payloads = await adapter.fetch()
        except Exception as exc:
            logger.exception("Market source adapter %s failed: %s", adapter.__class__.__name__, exc)
            continue
        for payload in payloads:
            signals.append(_upsert_signal(session, payload))
    if not signals:
        raise RuntimeError("No market signals were ingested.")
    return signals


def _latest_signals(session: Session, limit: int = 24) -> list[MarketSignal]:
    rows = session.exec(
        select(MarketSignal).order_by(MarketSignal.asOfDate.desc(), MarketSignal.id.desc()).limit(limit)
    ).all()
    unique: dict[tuple[str, str], MarketSignal] = {}
    for row in rows:
        unique.setdefault((row.metricKey, row.geography), row)
    return list(unique.values())


def _source_for_signal(session: Session, signal: MarketSignal) -> Optional[MarketDataSource]:
    return session.get(MarketDataSource, signal.sourceId)


def _signal_context(session: Session, signals: list[MarketSignal]) -> list[dict[str, Any]]:
    context = []
    for signal in signals:
        source = _source_for_signal(session, signal)
        context.append(
            {
                "id": signal.id,
                "metricKey": signal.metricKey,
                "label": signal.label,
                "geography": signal.geography,
                "value": signal.value,
                "unit": signal.unit,
                "asOfDate": signal.asOfDate.isoformat(),
                "changePct": signal.changePct,
                "confidence": signal.confidence,
                "source": {
                    "code": source.code if source else None,
                    "name": source.name if source else None,
                    "attribution": source.attribution if source else None,
                    "sourceUrl": source.sourceUrl if source else None,
                },
            }
        )
    return context


def _evaluate_narrative_eligibility(signals: list[MarketSignal]) -> str:
    if not signals:
        return "blocked_no_data"
    freshest = max(signal.asOfDate for signal in signals)
    age_days = (_today() - freshest).days
    if age_days <= 14:
        return "fresh"
    if age_days <= 45:
        return "stale_but_usable"
    return "blocked_stale_data"


def _format_value(value: float, unit: str) -> str:
    if unit == "percent":
        return f"{value:.2f}%"
    if unit == "usd":
        return f"${value:,.0f}"
    if unit == "homes":
        return f"{value:,.0f} homes"
    return f"{value:g} {unit}".strip()


def _fallback_market_insights(signals: list[MarketSignal]) -> dict[str, Any]:
    by_key = {signal.metricKey: signal for signal in signals}
    rate = by_key.get("mortgage_rate_30y_fixed")
    inventory = by_key.get("active_inventory")
    price = by_key.get("median_home_price")

    themes: list[dict[str, Any]] = []
    if rate:
        themes.append(
            {
                "title": "Mortgage Rates Are Driving This Week's Market Conversation",
                "headline": f"30-Year Rates: {_format_value(rate.value, rate.unit)}",
                "summary": "Use the latest rate reading to help buyers understand affordability and monthly-payment sensitivity.",
                "buyerMessage": "Buyers should pressure-test payments before shopping and update pre-approval numbers when rates move.",
                "sellerMessage": "Sellers should remember affordability still shapes buyer urgency, even when demand is healthy.",
                "cta": "Want to see what this means in your price range? Let's run the numbers.",
                "claims": [
                    {
                        "claimText": f"The latest 30-year fixed mortgage rate is {_format_value(rate.value, rate.unit)}.",
                        "sourceSignalId": rate.id,
                        "metricKey": rate.metricKey,
                        "expectedValue": rate.value,
                        "unit": rate.unit,
                    }
                ],
            }
        )
    if inventory:
        themes.append(
            {
                "title": "More Inventory Gives Consumers More Room To Compare",
                "headline": f"Inventory: {_format_value(inventory.value, inventory.unit)}",
                "summary": "Frame supply as practical context: more choices can help buyers compare, while sellers need sharper positioning.",
                "buyerMessage": "Buyers may have more room to compare options, but desirable homes can still move quickly.",
                "sellerMessage": "Sellers should price against today's active competition, not last year's market.",
                "cta": "I can help you compare the national story with your local inventory.",
                "claims": [
                    {
                        "claimText": f"Active inventory is {_format_value(inventory.value, inventory.unit)}.",
                        "sourceSignalId": inventory.id,
                        "metricKey": inventory.metricKey,
                        "expectedValue": inventory.value,
                        "unit": inventory.unit,
                    }
                ],
            }
        )
    if price:
        themes.append(
            {
                "title": "Prices Are Still Local, Even When The National Story Is Useful",
                "headline": f"Median Price: {_format_value(price.value, price.unit)}",
                "summary": "Use national price movement as a conversation starter, then move clients into local comps.",
                "buyerMessage": "Buyers should compare affordability by neighborhood, not just the national median.",
                "sellerMessage": "Sellers should anchor pricing to current local comparable sales.",
                "cta": "Ask me for a local pricing snapshot before making your next move.",
                "claims": [
                    {
                        "claimText": f"The configured national median home price is {_format_value(price.value, price.unit)}.",
                        "sourceSignalId": price.id,
                        "metricKey": price.metricKey,
                        "expectedValue": price.value,
                        "unit": price.unit,
                    }
                ],
            }
        )

    return {
        "marketSummary": "The current market story should stay practical: explain affordability, supply, and local variation without overpromising a forecast.",
        "cautions": [
            "National data may not match a client's local market.",
            "Avoid guarantees about rates, prices, or timing.",
        ],
        "themes": themes[:3],
    }


def _normalize_insights(payload: dict[str, Any], signals: list[MarketSignal]) -> dict[str, Any]:
    fallback = _fallback_market_insights(signals)
    themes = payload.get("themes")
    if not isinstance(themes, list) or not themes:
        return fallback
    valid_signal_ids = {signal.id for signal in signals if signal.id is not None}
    normalized_themes: list[dict[str, Any]] = []
    for theme in themes[:5]:
        if not isinstance(theme, dict):
            continue
        claims = []
        for claim in theme.get("claims", []):
            if not isinstance(claim, dict):
                continue
            source_signal_id = _coerce_int(claim.get("sourceSignalId"))
            if source_signal_id in valid_signal_ids:
                claim = dict(claim)
                claim["sourceSignalId"] = source_signal_id
                claims.append(claim)
        if not claims:
            continue
        normalized_themes.append(
            {
                "title": str(theme.get("title") or theme.get("headline") or "Market Update"),
                "headline": str(theme.get("headline") or theme.get("title") or "Market Update"),
                "summary": str(theme.get("summary") or ""),
                "buyerMessage": str(theme.get("buyerMessage") or ""),
                "sellerMessage": str(theme.get("sellerMessage") or ""),
                "cta": str(theme.get("cta") or "Message me for a local market read."),
                "claims": claims,
            }
        )
    if not normalized_themes:
        return fallback
    return {
        "marketSummary": str(payload.get("marketSummary") or fallback["marketSummary"]),
        "cautions": payload.get("cautions") if isinstance(payload.get("cautions"), list) else fallback["cautions"],
        "themes": normalized_themes,
    }


async def _generate_insights(session: Session, signals: list[MarketSignal]) -> dict[str, Any]:
    if os.getenv("MARKET_PIPELINE_DISABLE_LLM", "false").lower() == "true":
        return _fallback_market_insights(signals)

    context = _signal_context(session, signals)
    try:
        payload = await asyncio.wait_for(
            LLMService.generate_market_insights({"signals": context, "runDate": _today().isoformat()}),
            timeout=float(os.getenv("MARKET_PIPELINE_LLM_TIMEOUT_SECONDS", "20")),
        )
    except (asyncio.TimeoutError, GeminiConfigurationError, GeminiGenerationError) as exc:
        logger.info("Market insight LLM unavailable; using deterministic fallback: %s", exc)
        return _fallback_market_insights(signals)
    except Exception as exc:
        logger.warning("Market insight LLM failed unexpectedly; using deterministic fallback: %s", exc)
        return _fallback_market_insights(signals)
    return _normalize_insights(payload, signals)


def _source_citations(session: Session, signals: list[MarketSignal]) -> list[dict[str, Any]]:
    citations = []
    for signal in signals:
        source = _source_for_signal(session, signal)
        citations.append(
            {
                "signalId": signal.id,
                "metricKey": signal.metricKey,
                "label": signal.label,
                "value": signal.value,
                "unit": signal.unit,
                "asOfDate": signal.asOfDate.isoformat(),
                "sourceName": source.name if source else "Unknown source",
                "sourceUrl": source.sourceUrl if source else None,
                "attribution": source.attribution if source else None,
            }
        )
    return citations


def _validate_claim(session: Session, claim: dict[str, Any]) -> dict[str, Any]:
    signal_id = _coerce_int(claim.get("sourceSignalId"))
    signal = session.get(MarketSignal, signal_id) if signal_id is not None else None
    if signal is None:
        return {"status": "failed", "severity": "critical", "reason": "Source signal is missing."}
    expected = claim.get("expectedValue")
    if expected is None:
        return {"status": "failed", "severity": "critical", "reason": "Expected value is missing."}
    try:
        expected_value = float(expected)
    except (TypeError, ValueError):
        return {"status": "failed", "severity": "critical", "reason": "Expected value is not numeric."}
    tolerance = max(abs(signal.value) * 0.005, 0.01)
    if abs(signal.value - expected_value) > tolerance:
        return {
            "status": "failed",
            "severity": "critical",
            "reason": f"Claim value {expected_value:g} does not match source value {signal.value:g}.",
        }
    if claim.get("metricKey") and claim.get("metricKey") != signal.metricKey:
        return {"status": "failed", "severity": "critical", "reason": "Claim metric key does not match source signal."}
    return {"status": "verified", "severity": "info", "reason": "Claim matches source signal."}


def _persist_claims(
    session: Session,
    *,
    run: MarketInsightRun,
    artifact: MarketContentArtifact,
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    existing = session.exec(select(MarketClaim).where(MarketClaim.artifactId == artifact.id)).all()
    for row in existing:
        session.delete(row)
    session.commit()

    results = []
    has_critical = False
    for claim in claims:
        validation = _validate_claim(session, claim)
        has_critical = has_critical or validation["severity"] == "critical"
        source_signal_id = _coerce_int(claim.get("sourceSignalId"))
        signal = session.get(MarketSignal, source_signal_id) if source_signal_id is not None else None
        source = _source_for_signal(session, signal) if signal else None
        expected_value = None
        if claim.get("expectedValue") is not None:
            try:
                expected_value = float(claim["expectedValue"])
            except (TypeError, ValueError):
                expected_value = None
        row = MarketClaim(
            insightRunId=int(run.id),
            artifactId=artifact.id,
            sourceSignalId=signal.id if signal else None,
            claimText=str(claim.get("claimText") or ""),
            metricKey=claim.get("metricKey"),
            expectedValue=expected_value,
            unit=claim.get("unit"),
            validationStatus=validation["status"],
            severity=validation["severity"],
            sourceAttribution=source.attribution if source else None,
        )
        session.add(row)
        results.append({**claim, **validation})
    session.commit()
    return {
        "status": "blocked" if has_critical else "approved",
        "claims": results,
        "criticalIssueCount": sum(1 for result in results if result["severity"] == "critical"),
    }


def _upsert_artifact(
    session: Session,
    *,
    run: MarketInsightRun,
    theme: dict[str, Any],
    index: int,
    user_id: int,
    citations: list[dict[str, Any]],
) -> MarketContentArtifact:
    key = f"market-artifact:{run.id}:{index}:{user_id}"
    artifact = session.exec(
        select(MarketContentArtifact).where(MarketContentArtifact.idempotencyKey == key)
    ).first()
    content_json = {
        "theme": theme,
        "claims": theme.get("claims", []),
        "marketSummary": _json_loads(run.insightsJson, {}).get("marketSummary"),
        "disclaimer": "General market information only; not financial advice.",
    }
    caption = (
        f"{theme.get('summary', '').strip()}\n\n"
        f"Buyers: {theme.get('buyerMessage', '').strip()}\n"
        f"Sellers: {theme.get('sellerMessage', '').strip()}\n\n"
        f"{theme.get('cta', '').strip()}\n\n"
        "Data sources and dates are shown in the graphic."
    ).strip()
    now = _now()
    if artifact is None:
        artifact = MarketContentArtifact(
            user_id=user_id,
            insightRunId=int(run.id),
            title=str(theme.get("title") or "Market Update"),
            contentType="Static Post",
            platform="Instagram",
            status="qa_pending",
            headline=str(theme.get("headline") or theme.get("title") or "Market Update"),
            caption=caption,
            sourceCitationsJson=_json_dumps(citations),
            contentJson=_json_dumps(content_json),
            idempotencyKey=key,
        )
    else:
        artifact.title = str(theme.get("title") or "Market Update")
        artifact.headline = str(theme.get("headline") or theme.get("title") or "Market Update")
        artifact.caption = caption
        artifact.sourceCitationsJson = _json_dumps(citations)
        artifact.contentJson = _json_dumps(content_json)
        artifact.updatedAt = now
    session.add(artifact)
    session.commit()
    session.refresh(artifact)

    qa_report = _persist_claims(session, run=run, artifact=artifact, claims=theme.get("claims", []))
    artifact.qaReportJson = _json_dumps(qa_report)
    artifact.status = NEEDS_REVIEW_STATUS if qa_report["status"] == "blocked" else "asset_pending"
    artifact.updatedAt = _now()
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


def _queue_asset_generation(session: Session, artifact: MarketContentArtifact, user_id: int) -> Optional[int]:
    if artifact.status == NEEDS_REVIEW_STATUS:
        return None
    if media_generation_queue_enabled():
        job = create_media_generation_job(
            session,
            user_id=user_id,
            source_type="market_content_artifact",
            provider=INTERNAL_IMAGE_PROVIDER,
            model="market_intelligence.image_renderer",
            job_type="generate_market_image",
            payload={"artifactId": artifact.id},
            idempotency_key=f"market-content-artifact:{artifact.id}",
        )
        artifact.status = "queued_asset" if job.status != "completed" else READY_STATUS
        session.add(artifact)
        session.commit()
        return job.id

    from app.services.media_generation_service import generate_market_image_for_artifact

    generate_market_image_for_artifact(int(artifact.id), user_id=user_id)
    session.refresh(artifact)
    return None


async def run_market_pipeline(
    session: Session,
    *,
    user_id: int,
    force: bool = False,
    render_assets: bool = True,
) -> MarketInsightRun:
    run_date = _today()
    base_key = f"market-run:{run_date.isoformat()}:national:{user_id}"
    idempotency_key = f"{base_key}:{uuid4().hex}" if force else base_key
    existing = session.exec(
        select(MarketInsightRun).where(MarketInsightRun.idempotencyKey == idempotency_key)
    ).first()
    if existing is not None and not force:
        return existing

    run = MarketInsightRun(
        user_id=user_id,
        runDate=run_date,
        scope="national",
        status="running",
        qaStatus="pending",
        narrativeEligibility="unknown",
        idempotencyKey=idempotency_key,
    )
    session.add(run)
    session.commit()
    session.refresh(run)

    try:
        await ingest_market_signals(session)
        signals = _latest_signals(session)
        eligibility = _evaluate_narrative_eligibility(signals)
        run.narrativeEligibility = eligibility
        run.inputSignalIdsJson = _json_dumps([signal.id for signal in signals])
        run.sourceSummaryJson = _json_dumps(_signal_context(session, signals))
        if eligibility.startswith("blocked"):
            run.status = "needs_review"
            run.qaStatus = eligibility
            run.completedAt = _now()
            run.updatedAt = _now()
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

        insights = await _generate_insights(session, signals)
        run.insightsJson = _json_dumps(insights)
        run.status = "content_generated"
        run.updatedAt = _now()
        session.add(run)
        session.commit()
        session.refresh(run)

        citations = _source_citations(session, signals)
        artifacts = [
            _upsert_artifact(
                session,
                run=run,
                theme=theme,
                index=index,
                user_id=user_id,
                citations=citations,
            )
            for index, theme in enumerate(insights.get("themes", []))
        ]
        run.qaStatus = "approved" if all(artifact.status != NEEDS_REVIEW_STATUS for artifact in artifacts) else "needs_review"
        run.status = "asset_queued" if render_assets and run.qaStatus == "approved" else run.qaStatus
        run.completedAt = _now()
        run.updatedAt = _now()
        session.add(run)
        session.commit()
        session.refresh(run)

        if render_assets and run.qaStatus == "approved":
            for artifact in artifacts:
                _queue_asset_generation(session, artifact, user_id)
            refreshed_artifacts = [
                session.get(MarketContentArtifact, int(artifact.id))
                for artifact in artifacts
                if artifact.id is not None
            ]
            if all(
                artifact is not None and artifact.status in {READY_STATUS, "scheduled"}
                for artifact in refreshed_artifacts
            ):
                run.status = READY_STATUS
                run.updatedAt = _now()
                session.add(run)
                session.commit()
                session.refresh(run)
        return run
    except Exception as exc:
        logger.exception("Market pipeline run failed: %s", exc)
        run.status = "failed"
        run.qaStatus = "failed"
        run.lastError = str(exc)
        run.completedAt = _now()
        run.updatedAt = _now()
        session.add(run)
        session.commit()
        session.refresh(run)
        return run


def serialize_market_artifact(session: Session, artifact: MarketContentArtifact) -> dict[str, Any]:
    claims = session.exec(
        select(MarketClaim).where(MarketClaim.artifactId == artifact.id).order_by(MarketClaim.id)
    ).all()
    return {
        "id": artifact.id,
        "insightRunId": artifact.insightRunId,
        "scheduledPostId": artifact.scheduledPostId,
        "title": artifact.title,
        "contentType": artifact.contentType,
        "platform": artifact.platform,
        "status": artifact.status,
        "headline": artifact.headline,
        "caption": artifact.caption,
        "script": artifact.script,
        "imageUrl": artifact.imageUrl,
        "videoUrl": artifact.videoUrl,
        "sourceCitations": _json_loads(artifact.sourceCitationsJson, []),
        "qaReport": _json_loads(artifact.qaReportJson, {}),
        "content": _json_loads(artifact.contentJson, {}),
        "claims": [
            {
                "id": claim.id,
                "claimText": claim.claimText,
                "metricKey": claim.metricKey,
                "expectedValue": claim.expectedValue,
                "unit": claim.unit,
                "validationStatus": claim.validationStatus,
                "severity": claim.severity,
                "sourceSignalId": claim.sourceSignalId,
                "sourceAttribution": claim.sourceAttribution,
            }
            for claim in claims
        ],
        "createdAt": artifact.createdAt.isoformat(),
        "updatedAt": artifact.updatedAt.isoformat(),
    }


def serialize_market_run(session: Session, run: MarketInsightRun) -> dict[str, Any]:
    artifacts = session.exec(
        select(MarketContentArtifact)
        .where(MarketContentArtifact.insightRunId == run.id)
        .order_by(MarketContentArtifact.id)
    ).all()
    return {
        "id": run.id,
        "runDate": run.runDate.isoformat(),
        "scope": run.scope,
        "status": run.status,
        "qaStatus": run.qaStatus,
        "narrativeEligibility": run.narrativeEligibility,
        "sourceSummary": _json_loads(run.sourceSummaryJson, []),
        "inputSignalIds": _json_loads(run.inputSignalIdsJson, []),
        "insights": _json_loads(run.insightsJson, {}),
        "lastError": run.lastError,
        "startedAt": run.startedAt.isoformat(),
        "completedAt": run.completedAt.isoformat() if run.completedAt else None,
        "artifacts": [serialize_market_artifact(session, artifact) for artifact in artifacts],
    }


def schedule_market_artifact(
    session: Session,
    *,
    artifact_id: int,
    user_id: int,
    platform: str,
    scheduled_for: datetime,
) -> ScheduledPost:
    artifact = session.get(MarketContentArtifact, artifact_id)
    if artifact is None or artifact.user_id != user_id:
        raise ValueError("Market content artifact not found.")
    if artifact.status == "scheduled":
        existing_post = session.get(ScheduledPost, artifact.scheduledPostId) if artifact.scheduledPostId else None
        if existing_post is not None:
            return existing_post
        artifact.status = READY_STATUS
        artifact.scheduledPostId = None
        artifact.updatedAt = _now()
        session.add(artifact)
        session.commit()
        session.refresh(artifact)
    if artifact.status != READY_STATUS:
        raise ValueError("Market content artifact is not ready to schedule.")

    post = ScheduledPost(
        user_id=user_id,
        source="market_intelligence",
        title=artifact.title,
        contentType=artifact.contentType,
        generationStrategy="TIER_1_AUTO",
        content=artifact.caption or artifact.headline or artifact.title,
        imageUrl=artifact.imageUrl,
        videoUrl=artifact.videoUrl,
        scheduledFor=scheduled_for,
        platform=platform,
        status="Scheduled",
    )
    session.add(post)
    session.commit()
    session.refresh(post)

    artifact.scheduledPostId = post.id
    artifact.status = "scheduled"
    artifact.updatedAt = _now()
    session.add(artifact)
    session.commit()
    return post


async def run_scheduled_market_pipeline() -> int:
    processed = 0
    from app.database import engine

    with Session(engine) as session:
        users = session.exec(select(User).order_by(User.id)).all()
        for user in users:
            if user.id is None:
                continue
            await run_market_pipeline(session, user_id=int(user.id), force=False, render_assets=True)
            processed += 1
    return processed
