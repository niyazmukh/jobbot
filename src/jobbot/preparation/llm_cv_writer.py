"""Iterative LLM CV writer: draft -> reviewer -> final with evidence guardrails."""

from __future__ import annotations

import json
import time
from collections import deque
from threading import Lock
import urllib.error
import urllib.request
from dataclasses import dataclass

from sqlalchemy.orm import Session

from jobbot.config import get_settings
from jobbot.db.models import CandidateFact, CandidateProfile, Job
from jobbot.model_calls import get_prompt_version, record_model_call


_RPM_WINDOW_SECONDS = 60.0
_RPM_CALL_TIMESTAMPS: deque[float] = deque()
_RPM_LOCK = Lock()


@dataclass
class LlmCvWriterResult:
    """Result payload from iterative LLM CV generation."""

    markdown: str
    metadata: dict[str, object]


def llm_provider_ready() -> bool:
    """Return whether configured provider has required API key configured."""

    settings = get_settings()
    provider = (settings.llm_provider or "").strip().lower()
    if provider == "gemini":
        return bool(settings.gemini_api_key)
    if provider == "openai":
        return bool(settings.openai_api_key)
    if provider == "anthropic":
        return bool(settings.anthropic_api_key)
    return False


def build_iterative_llm_resume(
    session: Session,
    *,
    job: Job,
    candidate: CandidateProfile,
    facts: list[CandidateFact],
    score_json: dict,
) -> LlmCvWriterResult:
    """Generate resume markdown via iterative LLM drafting and review loop."""

    settings = get_settings()
    provider = (settings.llm_provider or "gemini").strip().lower()
    model_name = settings.llm_cv_writer_model
    reviewer_model = settings.llm_cv_reviewer_model or model_name

    fact_bank = [
        {
            "fact_key": fact.fact_key,
            "category": fact.category,
            "content": fact.content,
        }
        for fact in facts
    ]
    fact_keys = {row["fact_key"] for row in fact_bank}

    job_context = {
        "title": job.title,
        "company": job.company.name if job.company else "Unknown company",
        "canonical_url": job.canonical_url,
        "location": job.location_normalized,
        "description": job.description_text or "",
        "requirements_structured": job.requirements_structured or {},
        "score_summary": {
            "overall_score": score_json.get("overall_score"),
            "confidence_score": score_json.get("confidence_score"),
            "blocked": score_json.get("blocked"),
            "blocking_reasons": score_json.get("blocking_reasons", []),
            "matched_skills": score_json.get("matched_skills", []),
            "missing_skills": score_json.get("missing_skills", []),
            "seniority_matches": score_json.get("seniority_matches", []),
        },
    }

    candidate_context = {
        "name": candidate.name,
        "personal_details": candidate.personal_details or {},
        "target_preferences": candidate.target_preferences or {},
        "education": [row["content"] for row in fact_bank if row["category"] == "education"],
    }

    draft_prompt = _draft_prompt(
        candidate_context=candidate_context,
        job_context=job_context,
        fact_bank=fact_bank,
    )
    draft_json_text = _invoke_llm_json(
        session,
        stage="preparation_cv_draft",
        prompt_key="preparation_cv_draft",
        provider=provider,
        model_name=model_name,
        prompt_text=draft_prompt,
    )
    draft_doc = _parse_json_or_raise(draft_json_text)

    review_prompt = _review_prompt(
        candidate_context=candidate_context,
        job_context=job_context,
        draft_doc=draft_doc,
        fact_bank=fact_bank,
    )
    review_json_text = _invoke_llm_json(
        session,
        stage="preparation_cv_review",
        prompt_key="preparation_cv_review",
        provider=provider,
        model_name=reviewer_model,
        prompt_text=review_prompt,
    )
    review_doc = _parse_json_or_raise(review_json_text)

    final_prompt = _finalize_prompt(
        candidate_context=candidate_context,
        job_context=job_context,
        draft_doc=draft_doc,
        review_doc=review_doc,
        fact_bank=fact_bank,
    )
    final_json_text = _invoke_llm_json(
        session,
        stage="preparation_cv_finalize",
        prompt_key="preparation_cv_finalize",
        provider=provider,
        model_name=model_name,
        prompt_text=final_prompt,
    )
    final_doc = _parse_json_or_raise(final_json_text)

    sanitized = _sanitize_resume_json(final_doc, fact_keys=fact_keys)
    markdown = _render_resume_markdown_from_json(sanitized)

    metadata = {
        "provider": provider,
        "model_name": model_name,
        "reviewer_model": reviewer_model,
        "quality_review": review_doc,
        "structured_resume": sanitized,
    }
    return LlmCvWriterResult(markdown=markdown, metadata=metadata)


def _invoke_llm_json(
    session: Session,
    *,
    stage: str,
    prompt_key: str,
    provider: str,
    model_name: str,
    prompt_text: str,
) -> str:
    """Invoke provider API and record telemetry."""

    _respect_llm_rpm_limit()
    started = time.perf_counter()
    provider_result = _provider_invoke_json(
        provider=provider,
        model_name=model_name,
        prompt_text=prompt_text,
    )
    if isinstance(provider_result, tuple):
        output_text, resolved_model_name = provider_result
    else:
        output_text, resolved_model_name = provider_result, model_name
    latency_ms = int((time.perf_counter() - started) * 1000)

    record_model_call(
        session,
        stage=stage,
        model_provider=provider,
        model_name=resolved_model_name,
        prompt_version=get_prompt_version(prompt_key),
        input_size=len(prompt_text),
        output_size=len(output_text),
        latency_ms=latency_ms,
        estimated_cost=None,
    )
    return output_text


def _respect_llm_rpm_limit() -> None:
    """Throttle outbound LLM provider calls to configured per-minute rate."""

    settings = get_settings()
    max_calls = max(1, int(getattr(settings, "llm_api_rpm", 5)))

    while True:
        now = time.monotonic()
        with _RPM_LOCK:
            while _RPM_CALL_TIMESTAMPS and (now - _RPM_CALL_TIMESTAMPS[0]) >= _RPM_WINDOW_SECONDS:
                _RPM_CALL_TIMESTAMPS.popleft()

            if len(_RPM_CALL_TIMESTAMPS) < max_calls:
                _RPM_CALL_TIMESTAMPS.append(now)
                return

            wait_seconds = _RPM_WINDOW_SECONDS - (now - _RPM_CALL_TIMESTAMPS[0])

        time.sleep(max(wait_seconds, 0.01))


def _provider_invoke_json(*, provider: str, model_name: str, prompt_text: str) -> tuple[str, str]:
    settings = get_settings()
    temperature = float(settings.llm_cv_writer_temperature)
    max_tokens = int(settings.llm_cv_writer_max_tokens)

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise ValueError("gemini_api_key_not_configured")
        primary_model_name = _normalize_gemini_model_name(model_name)
        fallback_raw = str(getattr(settings, "llm_cv_writer_fallback_model", "") or "").strip()
        fallback_model_name = _normalize_gemini_model_name(fallback_raw) if fallback_raw else ""
        candidate_models = [primary_model_name]
        if fallback_model_name and fallback_model_name not in candidate_models:
            candidate_models.append(fallback_model_name)

        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "maxOutputTokens": max_tokens,
            },
        }
        last_error: ValueError | None = None
        for candidate_model in candidate_models:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/{candidate_model}:generateContent"
                f"?key={settings.gemini_api_key}"
            )
            try:
                response = _http_post_json(url=url, payload=payload, headers={})
                text = (
                    ((response.get("candidates") or [{}])[0].get("content") or {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                return text, candidate_model
            except ValueError as exc:
                last_error = exc
                if _is_model_unavailable_error(str(exc)) and candidate_model != candidate_models[-1]:
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise ValueError("llm_provider_unexpected_error")

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("openai_api_key_not_configured")
        url = "https://api.openai.com/v1/chat/completions"
        payload = {
            "model": model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt_text},
            ],
        }
        response = _http_post_json(
            url=url,
            payload=payload,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        )
        return ((response.get("choices") or [{}])[0].get("message") or {}).get("content", ""), model_name

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("anthropic_api_key_not_configured")
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": model_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt_text}],
        }
        response = _http_post_json(
            url=url,
            payload=payload,
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        parts = response.get("content") or []
        if parts and isinstance(parts[0], dict):
            return str(parts[0].get("text") or ""), model_name
        return "", model_name

    raise ValueError("unsupported_llm_provider")


def _normalize_gemini_model_name(model_name: str) -> str:
    """Normalize known Gemini aliases to API-supported model identifiers."""

    normalized = model_name.strip()
    if normalized.startswith("models/"):
        normalized = normalized[len("models/"):]

    alias_map = {
        "gemini-3.0-flash": "gemini-3-flash-preview",
        "gemini-3-flash": "gemini-3-flash-preview",
        "gemini-3.0-flash-preview": "gemini-3-flash-preview",
    }
    return alias_map.get(normalized, normalized)


def _is_model_unavailable_error(error_text: str) -> bool:
    """Return whether provider error implies a model identifier is unavailable."""

    lowered = error_text.lower()
    return "llm_provider_http_error:404" in lowered or "not found" in lowered


def _http_post_json(*, url: str, payload: dict, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - network dependent
        detail = exc.read().decode("utf-8", errors="ignore")
        raise ValueError(f"llm_provider_http_error:{exc.code}:{detail[:300]}") from exc
    return json.loads(raw)


def _parse_json_or_raise(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        extracted = _extract_first_json_object(text)
        if extracted is not None:
            return extracted
        raise ValueError("llm_invalid_json_response") from exc


def _extract_first_json_object(text: str) -> dict | None:
    """Extract first parseable JSON object from mixed model output."""

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                    continue
                if char == "\\":
                    escape = True
                    continue
                if char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break

        start = text.find("{", start + 1)
    return None


def _sanitize_resume_json(doc: dict, *, fact_keys: set[str]) -> dict:
    """Keep only schema-relevant fields and evidence keys grounded in fact bank."""

    contact = doc.get("contact") if isinstance(doc.get("contact"), dict) else {}

    def _safe_list_str(key: str, limit: int) -> list[str]:
        rows = doc.get(key)
        if not isinstance(rows, list):
            return []
        return [str(item).strip() for item in rows[:limit] if str(item).strip()]

    summary = _safe_list_str("professional_summary", 6)
    education = _safe_list_str("education", 8)
    skills = _safe_list_str("skills", 20)

    experience_rows = []
    for row in doc.get("experience", []) if isinstance(doc.get("experience"), list) else []:
        if not isinstance(row, dict):
            continue
        bullets = []
        raw_bullets = row.get("bullets") if isinstance(row.get("bullets"), list) else []
        for bullet in raw_bullets[:8]:
            if not isinstance(bullet, dict):
                continue
            text = str(bullet.get("text") or "").strip()
            evidence = [
                str(key)
                for key in (bullet.get("evidence_fact_keys") or [])
                if str(key) in fact_keys
            ]
            if text and evidence:
                bullets.append({"text": text, "evidence_fact_keys": evidence})
        if bullets:
            experience_rows.append(
                {
                    "employer": str(row.get("employer") or "Unknown employer").strip(),
                    "role": str(row.get("role") or "").strip(),
                    "period": str(row.get("period") or "").strip(),
                    "bullets": bullets,
                }
            )

    alignment_rows = []
    for row in doc.get("requirement_alignment", []) if isinstance(doc.get("requirement_alignment"), list) else []:
        if not isinstance(row, dict):
            continue
        requirement = str(row.get("requirement") or "").strip()
        coverage = str(row.get("coverage") or "partial").strip().lower()
        if coverage not in {"strong", "partial", "gap"}:
            coverage = "partial"
        evidence = [
            str(item).strip()
            for item in (row.get("evidence") or [])
            if str(item).strip()
        ][:5]
        if requirement:
            alignment_rows.append(
                {
                    "requirement": requirement,
                    "coverage": coverage,
                    "evidence": evidence,
                }
            )

    gap_notes = _safe_list_str("gap_notes", 8)

    return {
        "contact": {
            "name": str(contact.get("name") or "").strip(),
            "email": str(contact.get("email") or "").strip(),
            "phone": str(contact.get("phone") or "").strip(),
            "linkedin": str(contact.get("linkedin") or "").strip(),
            "location": str(contact.get("location") or "").strip(),
        },
        "target": {
            "role": str((doc.get("target") or {}).get("role") or "").strip(),
            "company": str((doc.get("target") or {}).get("company") or "").strip(),
        },
        "professional_summary": summary,
        "experience": experience_rows,
        "education": education,
        "skills": skills,
        "requirement_alignment": alignment_rows,
        "gap_notes": gap_notes,
    }


def _render_resume_markdown_from_json(doc: dict) -> str:
    contact = doc.get("contact") or {}
    target = doc.get("target") or {}

    summary = "\n".join(f"- {row}" for row in (doc.get("professional_summary") or [])) or "- Summary unavailable"
    education = "\n".join(f"- {row}" for row in (doc.get("education") or [])) or "- Education unavailable"
    skills = "\n".join(f"- {row}" for row in (doc.get("skills") or [])) or "- Skills unavailable"

    exp_blocks: list[str] = []
    for row in doc.get("experience") or []:
        header = " | ".join(
            token
            for token in [row.get("employer") or "", row.get("role") or "", row.get("period") or ""]
            if token
        )
        bullet_lines = "\n".join(f"  - {item['text']}" for item in row.get("bullets") or [])
        exp_blocks.append(f"- {header}\n{bullet_lines}" if bullet_lines else f"- {header}")
    experience = "\n".join(exp_blocks) or "- Experience unavailable"

    align_rows = []
    for row in doc.get("requirement_alignment") or []:
        evidence = ", ".join(row.get("evidence") or []) or "No direct evidence listed"
        align_rows.append(
            f"- [{row.get('coverage', 'partial')}] {row.get('requirement')}: {evidence}"
        )
    alignment = "\n".join(align_rows) or "- Requirement alignment unavailable"

    gap_notes = "\n".join(f"- {row}" for row in (doc.get("gap_notes") or [])) or "- No explicit gap notes"

    return (
        f"# {contact.get('name') or 'Resume Variant'}\n\n"
        f"## Contact Information\n"
        f"- Email: {contact.get('email') or 'Not provided'}\n"
        f"- Phone: {contact.get('phone') or 'Not provided'}\n"
        f"- LinkedIn: {contact.get('linkedin') or 'Not provided'}\n"
        f"- Location: {contact.get('location') or 'Not provided'}\n\n"
        f"## Target Role\n"
        f"- Role: {target.get('role') or 'Not specified'}\n"
        f"- Company: {target.get('company') or 'Not specified'}\n\n"
        f"## Professional Summary\n{summary}\n\n"
        f"## Professional Experience\n{experience}\n\n"
        f"## Education\n{education}\n\n"
        f"## Skills\n{skills}\n\n"
        f"## Requirements Alignment\n{alignment}\n\n"
        f"## Gap Notes\n{gap_notes}\n"
    )


def _draft_prompt(*, candidate_context: dict, job_context: dict, fact_bank: list[dict]) -> str:
    return (
        "STAGE: DRAFT\n"
        "Create a highly relevant job-targeted resume in JSON using only supplied facts. "
        "Do not invent employers, achievements, skills, or dates.\n"
        "Return JSON object with keys: "
        "contact, target, professional_summary(list), experience(list), education(list), skills(list), "
        "requirement_alignment(list), gap_notes(list).\n"
        "Each experience bullet must be an object {text, evidence_fact_keys:[...]} and evidence_fact_keys "
        "must reference provided fact_key values.\n\n"
        f"Candidate context:\n{json.dumps(candidate_context, ensure_ascii=True)}\n\n"
        f"Job context:\n{json.dumps(job_context, ensure_ascii=True)}\n\n"
        f"Fact bank:\n{json.dumps(fact_bank, ensure_ascii=True)}\n"
    )


def _review_prompt(*, candidate_context: dict, job_context: dict, draft_doc: dict, fact_bank: list[dict]) -> str:
    return (
        "STAGE: REVIEW\n"
        "You are a strict CV quality reviewer optimizing interview conversion while preventing fabrication.\n"
        "Review the draft JSON resume for relevance to job requirements, clarity, credibility, and ATS readability.\n"
        "Return JSON object with keys: issues(list), coverage_score(int 0-100), conversion_recommendations(list).\n"
        "Each issue must include severity(high|medium|low), issue, fix_instruction.\n\n"
        f"Candidate context:\n{json.dumps(candidate_context, ensure_ascii=True)}\n\n"
        f"Job context:\n{json.dumps(job_context, ensure_ascii=True)}\n\n"
        f"Fact bank:\n{json.dumps(fact_bank, ensure_ascii=True)}\n\n"
        f"Draft resume JSON:\n{json.dumps(draft_doc, ensure_ascii=True)}\n"
    )


def _finalize_prompt(*, candidate_context: dict, job_context: dict, draft_doc: dict, review_doc: dict, fact_bank: list[dict]) -> str:
    return (
        "STAGE: FINALIZE\n"
        "Rewrite the resume JSON by applying reviewer feedback while preserving factual grounding only from provided facts.\n"
        "Return JSON object with keys: contact, target, professional_summary(list), experience(list), education(list), "
        "skills(list), requirement_alignment(list), gap_notes(list).\n"
        "Do not output markdown. Output strict JSON only.\n\n"
        f"Candidate context:\n{json.dumps(candidate_context, ensure_ascii=True)}\n\n"
        f"Job context:\n{json.dumps(job_context, ensure_ascii=True)}\n\n"
        f"Fact bank:\n{json.dumps(fact_bank, ensure_ascii=True)}\n\n"
        f"Draft resume JSON:\n{json.dumps(draft_doc, ensure_ascii=True)}\n\n"
        f"Reviewer feedback JSON:\n{json.dumps(review_doc, ensure_ascii=True)}\n"
    )
