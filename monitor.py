from __future__ import annotations

import argparse
import json
import logging
import os
import re
import smtplib
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests


HH_API_URL = "https://api.hh.ru/vacancies"
SUPERJOB_API_URL = "https://api.superjob.ru/2.0/vacancies/"


def build_hh_session_headers(config: dict[str, Any]) -> dict[str, str]:
    """HH requires a descriptive User-Agent, usually: AppName/1.0 (you@email.com)."""
    hh_cfg = config.get("hh_api") or {}
    ua = (
        os.environ.get("HH_USER_AGENT", "").strip()
        or str(hh_cfg.get("user_agent", "")).strip()
        or "job-monitor/1.0 (replace-with-your-email@example.com)"
    )
    token = os.environ.get("HH_ACCESS_TOKEN", "").strip()
    headers: dict[str, str] = {"User-Agent": ua}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def superjob_unix_to_iso(ts: int | None) -> str:
    if not ts:
        return now_utc().isoformat()
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def superjob_salary_text(obj: dict[str, Any]) -> str:
    if obj.get("agreement"):
        return "по договоренности"
    pf = int(obj.get("payment_from") or 0)
    pt = int(obj.get("payment_to") or 0)
    cur = (obj.get("currency") or "rub").upper()
    if pf and pt:
        return f"{pf}-{pt} {cur}"
    if pf:
        return f"from {pf} {cur}"
    if pt:
        return f"up to {pt} {cur}"
    return "n/a"


def superjob_max_rub(obj: dict[str, Any]) -> int:
    if obj.get("agreement"):
        return 0
    cur = (obj.get("currency") or "rub").lower()
    if cur not in ("rub", "rur", ""):
        return 0
    return int(obj.get("payment_to") or obj.get("payment_from") or 0)


def superjob_to_scam_raw(obj: dict[str, Any]) -> dict[str, Any]:
    pf = int(obj.get("payment_from") or 0)
    pt = int(obj.get("payment_to") or 0)
    salary = None
    if not obj.get("agreement") and (pf or pt):
        salary = {"currency": "RUR", "from": pf, "to": pt or pf, "gross": False}
    exp_id = (obj.get("experience") or {}).get("id")
    try:
        exp_num = int(exp_id) if exp_id is not None else -1
    except (TypeError, ValueError):
        exp_num = -1
    exp_hh = {1: "noExperience", 2: "between1And3", 3: "between3And6", 4: "moreThan6"}.get(exp_num, "")
    return {
        "salary": salary,
        "employer": {"name": obj.get("firm_name") or "n/a", "trusted": False},
        "experience": {"id": exp_hh},
        "snippet": {
            "requirement": (obj.get("candidat") or "")[:800],
            "responsibility": (obj.get("work") or "")[:800],
        },
    }


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def normalize_smtp_host(raw: str) -> str:
    h = (raw or "").strip()
    if not h:
        return ""
    if "://" in h:
        from urllib.parse import urlparse

        parsed = urlparse(h if "://" in h else f"https://{h}")
        if parsed.hostname:
            return parsed.hostname.strip()
    return h.split("/")[0].strip()


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_text(value: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", value or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean.lower()


def contains_any(text: str, phrases: list[str]) -> list[str]:
    found: list[str] = []
    for phrase in phrases:
        if phrase and phrase.lower() in text:
            found.append(phrase)
    return found


def salary_to_text(salary: dict[str, Any] | None) -> str:
    if not salary:
        return "n/a"
    cur = salary.get("currency") or ""
    gross = "gross" if salary.get("gross") else "net"
    from_v = salary.get("from")
    to_v = salary.get("to")
    if from_v and to_v:
        return f"{from_v}-{to_v} {cur} ({gross})"
    if from_v:
        return f"from {from_v} {cur} ({gross})"
    if to_v:
        return f"up to {to_v} {cur} ({gross})"
    return f"salary in {cur}".strip()


def salary_max_rub(salary: dict[str, Any] | None) -> int:
    if not salary:
        return 0
    if salary.get("currency") != "RUR":
        return 0
    return int(salary.get("to") or salary.get("from") or 0)


@dataclass
class VacancyRecord:
    vacancy_id: str
    name: str
    employer: str
    salary_text: str
    salary_max_rub: int
    schedule: str
    employment: str
    published_at: str
    link: str
    snippet: str
    fit_score: int
    risk_score: int
    risk_level: str
    risk_reasons: list[str]

    def to_state(self) -> dict[str, Any]:
        return {
            "id": self.vacancy_id,
            "name": self.name,
            "employer": self.employer,
            "salary": self.salary_text,
            "salary_max_rub": self.salary_max_rub,
            "schedule": self.schedule,
            "employment": self.employment,
            "published_at": self.published_at,
            "link": self.link,
            "description": self.snippet,
            "ai_fit_percent": self.fit_score,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "risk_reasons": self.risk_reasons,
        }


class JobMonitor:
    def __init__(self, config: dict[str, Any], disable_email: bool = False, dry_run: bool = False) -> None:
        self.config = config
        self.disable_email = disable_email
        self.dry_run = dry_run
        self.state_file = Path(config["storage"]["state_file"])
        self.requests_timeout = int(config.get("network", {}).get("timeout_sec", 20))
        self.provider = (config.get("provider") or "superjob").strip().lower()
        self.session = requests.Session()
        if self.provider == "hh":
            self.session.headers.update(build_hh_session_headers(config))
        elif self.provider == "superjob":
            sj = config.get("superjob") or {}
            app_id = os.environ.get("SUPERJOB_APP_ID", "").strip() or str(sj.get("app_id", "")).strip()
            if not app_id:
                raise ValueError(
                    "Superjob: set repository secret SUPERJOB_APP_ID or superjob.app_id in config.json "
                    "(register at https://api.superjob.ru/register)."
                )
            self.superjob_app_id = app_id
            self.session.headers.update(
                {
                    "User-Agent": "job-monitor/1.0 (superjob)",
                    "X-Api-App-Id": app_id,
                }
            )
        else:
            raise ValueError(f"Unknown provider: {self.provider}. Use 'superjob' or 'hh'.")

    def run(self) -> None:
        state = load_json(self.state_file, {"vacancies": {}, "updated_at": None})
        active_before = {
            vid
            for vid, row in state["vacancies"].items()
            if row.get("active", False)
        }

        fetched = self.fetch_vacancies()
        current_ids = {item.vacancy_id for item in fetched}
        now_iso = now_utc().isoformat()

        for item in fetched:
            row = item.to_state()
            prev = state["vacancies"].get(item.vacancy_id, {})
            row["first_seen"] = prev.get("first_seen", now_iso)
            row["last_seen"] = now_iso
            row["active"] = True
            state["vacancies"][item.vacancy_id] = row

        self.deactivate_missing(state, current_ids)
        state["updated_at"] = now_iso
        save_json(self.state_file, state)

        active_after = self.get_active_sorted(state)
        new_items = [v for v in active_after if v["id"] not in active_before]

        logging.info("Run done. Active=%s, New=%s", len(active_after), len(new_items))
        self.send_report_if_needed(active_after, new_items)

    def fetch_vacancies(self) -> list[VacancyRecord]:
        if self.provider == "superjob":
            return self.fetch_superjob_vacancies()
        return self.fetch_hh_vacancies()

    def fetch_hh_vacancies(self) -> list[VacancyRecord]:
        search = self.config["search"]
        fit_cfg = self.config["fit_keywords"]
        risk_cfg = self.config["risk"]
        params_base = {
            "text": search["query"],
            "per_page": search.get("per_page", 50),
            "order_by": "publication_time",
            "search_field": "name",
            "only_with_salary": search.get("only_with_salary", False),
        }

        max_pages = int(search.get("pages", 2))
        lookback_hours = int(search.get("lookback_hours", 24))
        min_salary = int(search.get("min_salary_rub", 0))
        min_fit = int(fit_cfg.get("min_fit_percent", 40))
        max_risk = int(risk_cfg.get("max_risk_score", 45))
        cutoff = now_utc() - timedelta(hours=lookback_hours)

        items: list[VacancyRecord] = []
        seen_ids: set[str] = set()

        for page in range(max_pages):
            params = dict(params_base)
            params["page"] = page
            resp = self.session.get(HH_API_URL, params=params, timeout=self.requests_timeout)
            if not resp.ok:
                logging.error(
                    "HH API HTTP %s for %s — body (truncated): %s",
                    resp.status_code,
                    resp.url,
                    (resp.text or "")[:2000],
                )
            resp.raise_for_status()
            payload = resp.json()

            for raw in payload.get("items", []):
                vid = str(raw.get("id"))
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)

                published = parse_iso_dt(raw["published_at"])
                if published < cutoff:
                    continue

                schedule = ((raw.get("schedule") or {}).get("name") or "").lower()
                employment = ((raw.get("employment") or {}).get("name") or "").lower()
                if not self.schedule_ok(schedule, search.get("allowed_schedule", [])):
                    continue
                if not self.employment_ok(employment, search.get("allowed_employment", [])):
                    continue

                salary = raw.get("salary")
                max_salary = salary_max_rub(salary)
                if min_salary and max_salary and max_salary < min_salary:
                    continue

                name = raw.get("name", "")
                employer = (raw.get("employer") or {}).get("name", "n/a")
                snippet = " ".join(
                    [
                        (raw.get("snippet") or {}).get("requirement") or "",
                        (raw.get("snippet") or {}).get("responsibility") or "",
                    ]
                ).strip()
                text_for_checks = normalize_text(f"{name} {snippet}")

                fit_score = self.ai_fit_score(text_for_checks, fit_cfg)
                risk_score, risk_level, risk_reasons = self.scam_score(text_for_checks, raw, risk_cfg)
                if fit_score < min_fit or risk_score > max_risk:
                    continue

                items.append(
                    VacancyRecord(
                        vacancy_id=vid,
                        name=name,
                        employer=employer,
                        salary_text=salary_to_text(salary),
                        salary_max_rub=max_salary,
                        schedule=(raw.get("schedule") or {}).get("name") or "n/a",
                        employment=(raw.get("employment") or {}).get("name") or "n/a",
                        published_at=raw.get("published_at"),
                        link=raw.get("alternate_url", ""),
                        snippet=re.sub(r"\s+", " ", snippet)[:700],
                        fit_score=fit_score,
                        risk_score=risk_score,
                        risk_level=risk_level,
                        risk_reasons=risk_reasons,
                    )
                )

        items.sort(key=lambda x: (x.fit_score, x.published_at), reverse=True)
        return items

    def fetch_superjob_vacancies(self) -> list[VacancyRecord]:
        search = self.config["search"]
        sj_cfg = self.config.get("superjob") or {}
        fit_cfg = self.config["fit_keywords"]
        risk_cfg = self.config["risk"]

        keyword = (
            os.environ.get("SUPERJOB_KEYWORD", "").strip()
            or str(search.get("superjob_keyword") or search.get("keyword") or "").strip()
            or re.sub(r"\s+", " ", str(search.get("query", "")).replace("(", " ").replace(")", " "))[:200]
        )
        if not keyword:
            raise ValueError("Superjob: set search.superjob_keyword or search.keyword in config.json")

        lookback_hours = int(search.get("lookback_hours", 24))
        if lookback_hours <= 24:
            period = 1
        elif lookback_hours <= 72:
            period = 3
        elif lookback_hours <= 168:
            period = 7
        else:
            period = 0

        max_pages = int(search.get("pages", 3))
        per_page = min(int(search.get("per_page", 50)), 100)
        min_salary = int(search.get("min_salary_rub", 0))
        min_fit = int(fit_cfg.get("min_fit_percent", 40))
        max_risk = int(risk_cfg.get("max_risk_score", 45))
        cutoff = now_utc() - timedelta(hours=lookback_hours)

        remote_only = bool(sj_cfg.get("remote_only", True))
        place_of_work = sj_cfg.get("place_of_work")
        if place_of_work is None:
            place_of_work = 2 if remote_only else None

        items: list[VacancyRecord] = []
        seen_ids: set[str] = set()

        for page in range(max_pages):
            params: dict[str, Any] = {
                "keyword": keyword,
                "order_field": "date",
                "order_direction": "desc",
                "page": page,
                "count": per_page,
                "period": period,
            }
            if search.get("only_with_salary"):
                params["no_agreement"] = 1
            if min_salary:
                params["payment_from"] = min_salary
            if place_of_work is not None:
                params["place_of_work"] = int(place_of_work)

            resp = self.session.get(SUPERJOB_API_URL, params=params, timeout=self.requests_timeout)
            if not resp.ok:
                logging.error(
                    "Superjob API HTTP %s — body (truncated): %s",
                    resp.status_code,
                    (resp.text or "")[:2000],
                )
            resp.raise_for_status()
            payload = resp.json()
            objects = payload.get("objects") or []
            logging.info("Superjob page %s: API returned %s vacancies (more=%s)", page, len(objects), payload.get("more"))

            for raw in objects:
                oid = raw.get("id")
                if oid is None:
                    continue
                vid = f"sj_{oid}"
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)

                pub_ts = raw.get("date_published")
                published = superjob_unix_to_iso(int(pub_ts)) if pub_ts else now_utc().isoformat()
                try:
                    if parse_iso_dt(published) < cutoff:
                        continue
                except Exception:
                    pass

                pow_obj = raw.get("place_of_work") or {}
                schedule_title = (pow_obj.get("title")) or "n/a"
                if pow_obj.get("id") == 2:
                    schedule_check = "удаленная работа " + schedule_title.lower()
                else:
                    schedule_check = schedule_title.lower()
                employment_title = ((raw.get("type_of_work") or {}).get("title")) or "n/a"
                employment_check = employment_title.lower()

                if not self.schedule_ok(schedule_check, search.get("allowed_schedule", [])):
                    continue
                if not self.employment_ok(employment_check, search.get("allowed_employment", [])):
                    continue

                max_rub = superjob_max_rub(raw)
                if min_salary and max_rub and max_rub < min_salary:
                    continue

                name = raw.get("profession") or "n/a"
                employer = raw.get("firm_name") or "n/a"
                snippet = " ".join([raw.get("candidat") or "", raw.get("work") or ""]).strip()
                scam_raw = superjob_to_scam_raw(raw)
                text_for_checks = normalize_text(f"{name} {snippet}")
                if pow_obj.get("id") == 2:
                    text_for_checks = normalize_text(
                        f"{text_for_checks} удаленная работа дистанционно на дому"
                    )

                fit_score = self.ai_fit_score(text_for_checks, fit_cfg)
                risk_score, risk_level, risk_reasons = self.scam_score(text_for_checks, scam_raw, risk_cfg)
                if fit_score < min_fit or risk_score > max_risk:
                    continue

                items.append(
                    VacancyRecord(
                        vacancy_id=vid,
                        name=name,
                        employer=employer,
                        salary_text=superjob_salary_text(raw),
                        salary_max_rub=max_rub,
                        schedule=schedule_title,
                        employment=employment_title,
                        published_at=published,
                        link=raw.get("link") or "",
                        snippet=re.sub(r"\s+", " ", snippet)[:700],
                        fit_score=fit_score,
                        risk_score=risk_score,
                        risk_level=risk_level,
                        risk_reasons=risk_reasons,
                    )
                )

            if not payload.get("more"):
                break

        items.sort(key=lambda x: (x.fit_score, x.published_at), reverse=True)
        return items

    def ai_fit_score(self, text: str, fit_cfg: dict[str, Any]) -> int:
        must = [x.lower() for x in fit_cfg.get("must", []) if x]
        nice = [x.lower() for x in fit_cfg.get("nice", []) if x]
        exclude = [x.lower() for x in fit_cfg.get("exclude", []) if x]

        score = 0
        if must:
            per_must = 60 / max(len(must), 1)
            hits = sum(1 for kw in must if kw in text)
            score += int(hits * per_must)
        else:
            score += 40

        if nice:
            per_nice = 40 / max(len(nice), 1)
            hits = sum(1 for kw in nice if kw in text)
            score += int(hits * per_nice)
        else:
            score += 20

        exclude_hits = sum(1 for kw in exclude if kw in text)
        if exclude_hits:
            score -= min(35, exclude_hits * 12)

        return max(0, min(100, score))

    def scam_score(
        self,
        text: str,
        raw: dict[str, Any],
        risk_cfg: dict[str, Any],
    ) -> tuple[int, str, list[str]]:
        score = 0
        reasons: list[str] = []
        salary = raw.get("salary")
        employer = raw.get("employer") or {}

        if not salary:
            score += 10
            reasons.append("salary not specified")

        if employer and not employer.get("trusted", False):
            score += 15
            reasons.append("employer is not marked as trusted")

        high_salary_limit = int(risk_cfg.get("high_salary_without_exp_rub", 250000))
        experience = ((raw.get("experience") or {}).get("id") or "").strip()
        max_salary = salary_max_rub(salary)
        if experience in {"noExperience", "between1And3"} and max_salary > high_salary_limit:
            score += 15
            reasons.append("very high salary for low experience")

        phrase_penalties = risk_cfg.get("phrase_penalties", {})
        for phrase, penalty in phrase_penalties.items():
            if phrase.lower() in text:
                score += int(penalty)
                reasons.append(f"red flag phrase: {phrase}")

        market_account_flags = risk_cfg.get("marketplace_account_flags", [])
        found_account_flags = contains_any(text, [x.lower() for x in market_account_flags])
        if found_account_flags:
            score += 35
            reasons.append("asks to use personal marketplace account")

        score = max(0, min(100, score))
        if score >= 66:
            level = "high"
        elif score >= 36:
            level = "medium"
        else:
            level = "low"
        return score, level, reasons

    def schedule_ok(self, schedule: str, allowed: list[str]) -> bool:
        if not allowed:
            return True
        schedule = schedule.lower()
        allowed_lower = [x.lower() for x in allowed]
        return any(item in schedule for item in allowed_lower)

    def employment_ok(self, employment: str, allowed: list[str]) -> bool:
        if not allowed:
            return True
        employment = employment.lower()
        allowed_lower = [x.lower() for x in allowed]
        return any(item in employment for item in allowed_lower)

    def deactivate_missing(self, state: dict[str, Any], current_ids: set[str]) -> None:
        max_age_days = int(self.config["storage"].get("max_age_days", 7))
        now = now_utc()
        for vid, row in state["vacancies"].items():
            if not row.get("active", False):
                continue
            if vid in current_ids:
                continue

            is_stale = False
            try:
                published = parse_iso_dt(row["published_at"])
                if now - published > timedelta(days=max_age_days):
                    is_stale = True
            except Exception:
                pass

            if is_stale or self.vacancy_archived(vid):
                row["active"] = False
                row["removed_at"] = now.isoformat()

    def vacancy_archived(self, vacancy_id: str) -> bool:
        if self.provider == "superjob":
            eid = vacancy_id.removeprefix("sj_")
            url = f"{SUPERJOB_API_URL.rstrip('/')}/{eid}/"
            try:
                resp = self.session.get(url, timeout=self.requests_timeout)
                if resp.status_code == 404:
                    return True
                if not resp.ok:
                    return False
                payload = resp.json()
                return bool(payload.get("is_archive") or payload.get("is_storage"))
            except requests.RequestException:
                return False

        url = f"{HH_API_URL}/{vacancy_id}"
        try:
            resp = self.session.get(url, timeout=self.requests_timeout)
            if resp.status_code == 404:
                return True
            if not resp.ok:
                return False
            payload = resp.json()
            return bool(payload.get("archived", False))
        except requests.RequestException:
            return False

    def get_active_sorted(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        active = [row for row in state["vacancies"].values() if row.get("active", False)]
        active.sort(key=lambda x: (x.get("ai_fit_percent", 0), x.get("published_at", "")), reverse=True)
        return active

    def send_report_if_needed(self, active: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> None:
        email_cfg = self.config.get("email", {})
        digest_always = bool(email_cfg.get("digest_always", True))
        if self.disable_email:
            logging.info("Email disabled by flag.")
            return
        if not digest_always and not new_items:
            logging.info("No new vacancies and digest_always=false, email skipped.")
            return
        if self.dry_run:
            logging.info("Dry run enabled, email skipped.")
            return

        smtp_host = normalize_smtp_host(os.environ.get(email_cfg.get("smtp_host_env", "SMTP_HOST"), ""))
        port_env = os.environ.get(email_cfg.get("smtp_port_env", "SMTP_PORT"), "").strip()
        try:
            smtp_port = int(port_env) if port_env else 587
        except ValueError as exc:
            raise ValueError(f"Invalid SMTP_PORT value: {port_env!r}") from exc
        smtp_user = os.environ.get(email_cfg.get("smtp_user_env", "SMTP_USER"), "")
        smtp_pass = os.environ.get(email_cfg.get("smtp_pass_env", "SMTP_PASS"), "")
        from_addr = os.environ.get(email_cfg.get("smtp_from_env", "SMTP_FROM"), "")
        to_addr = os.environ.get(email_cfg.get("smtp_to_env", "SMTP_TO"), "")

        missing = [k for k, v in {
            "SMTP_HOST": smtp_host,
            "SMTP_PORT": str(smtp_port),
            "SMTP_USER": smtp_user,
            "SMTP_PASS": smtp_pass,
            "SMTP_FROM": from_addr,
            "SMTP_TO": to_addr,
        }.items() if not v]
        if missing:
            logging.warning("Email skipped. Missing env vars: %s", ", ".join(missing))
            return

        subject = f"Job Monitor: {len(new_items)} new, {len(active)} active"
        body = self.build_email_body(active, new_items)
        try:
            self.send_email(
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_pass=smtp_pass,
                from_addr=from_addr,
                to_addr=to_addr,
                subject=subject,
                body=body,
            )
        except OSError as exc:
            logging.error(
                "SMTP connection failed (%s:%s): %s. Check GitHub secret SMTP_HOST "
                "(use hostname only, e.g. smtp.gmail.com — not a URL, no spaces).",
                smtp_host,
                smtp_port,
                exc,
            )
            raise
        except smtplib.SMTPException as exc:
            logging.error("SMTP failed (%s:%s user=%s): %s", smtp_host, smtp_port, smtp_user, exc)
            logging.error("If Gmail: use App Password + 2FA; SMTP_HOST=smtp.gmail.com SMTP_PORT=587")
            raise
        logging.info("Email sent to %s", to_addr)

    def build_email_body(self, active: list[dict[str, Any]], new_items: list[dict[str, Any]]) -> str:
        lines = []
        lines.append(f"Generated at: {now_utc().isoformat()}")
        lines.append(f"New suitable vacancies: {len(new_items)}")
        lines.append(f"Total active suitable vacancies: {len(active)}")
        lines.append("")

        if new_items:
            lines.append("=== NEW TODAY ===")
            lines.extend(self.format_vacancy_lines(new_items))
            lines.append("")

        lines.append("=== ACTIVE LIST ===")
        lines.extend(self.format_vacancy_lines(active))
        lines.append("")
        lines.append("Note: active list keeps only currently visible and fresh vacancies.")
        return "\n".join(lines)

    def format_vacancy_lines(self, vacancies: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for idx, row in enumerate(vacancies, 1):
            risk = f"{row.get('risk_score', 0)} ({row.get('risk_level', 'n/a')})"
            out.append(f"{idx}. {row.get('name', 'n/a')} | {row.get('employer', 'n/a')}")
            out.append(f"   Salary: {row.get('salary', 'n/a')}")
            out.append(f"   Schedule: {row.get('schedule', 'n/a')} | Employment: {row.get('employment', 'n/a')}")
            out.append(f"   AI fit: {row.get('ai_fit_percent', 0)}% | Scam risk: {risk}")
            out.append(f"   Published: {row.get('published_at', 'n/a')}")
            out.append(f"   Link: {row.get('link', 'n/a')}")
            reasons = row.get("risk_reasons") or []
            if reasons:
                out.append(f"   Risk reasons: {', '.join(reasons)}")
            description = row.get("description", "")
            if description:
                out.append(f"   Description: {description[:300]}")
            out.append("")
        return out

    @staticmethod
    def send_email(
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_pass: str,
        from_addr: str,
        to_addr: str,
        subject: str,
        body: str,
    ) -> None:
        message = MIMEMultipart()
        message["From"] = from_addr
        message["To"] = to_addr
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_addr], message.as_string())


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def load_config(path: Path) -> dict[str, Any]:
    data = load_json(path, None)
    if not data:
        raise FileNotFoundError(
            f"Config file not found or empty: {path}. Copy config.example.json to config.json and edit it."
        )
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily vacancy monitor with anti-scam filtering.")
    parser.add_argument("--config", default="config.json", help="Path to config JSON file.")
    parser.add_argument("--dotenv", default=".env", help="Path to .env file with SMTP credentials.")
    parser.add_argument("--disable-email", action="store_true", help="Skip email sending.")
    parser.add_argument("--dry-run", action="store_true", help="Run logic without sending email.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv_file(Path(args.dotenv))
    config = load_config(Path(args.config))
    setup_logging(Path(config.get("logging", {}).get("file", "logs/monitor.log")))

    monitor = JobMonitor(config=config, disable_email=args.disable_email, dry_run=args.dry_run)
    try:
        monitor.run()
    except Exception:
        logging.error("Monitor failed:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
