from __future__ import annotations

import json
import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import jwt
import requests
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def build_jwt_token(project_id: str, credential_id: str, private_key: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": project_id,
        "iat": int((now - timedelta(seconds=30)).timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
    }
    headers = {"kid": credential_id}
    return jwt.encode(payload, private_key, algorithm="EdDSA", headers=headers)


def request_json(api_host: str, path: str, token: str, params: dict[str, Any]) -> dict[str, Any]:
    base = api_host.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if str(data.get("code", "")) != "200":
        raise RuntimeError(f"和风天气接口返回异常: {json.dumps(data, ensure_ascii=False)}")
    return data


def lookup_location(api_host: str, token: str, location_query: str, lang: str) -> dict[str, Any]:
    data = request_json(
        api_host,
        "/geo/v2/city/lookup",
        token,
        {"location": location_query, "number": 1, "lang": lang},
    )
    locations = data.get("location") or []
    if not locations:
        raise RuntimeError(f"未找到城市: {location_query}")
    return locations[0]


def fetch_forecast(api_host: str, token: str, location_id: str, lang: str, unit: str) -> list[dict[str, Any]]:
    data = request_json(
        api_host,
        "/v7/weather/3d",
        token,
        {"location": location_id, "lang": lang, "unit": unit},
    )
    daily = data.get("daily") or []
    if not daily:
        raise RuntimeError("未来3天天气数据为空")
    return daily[:3]


def format_mail(city_name: str, adm1: str, country: str, days: list[dict[str, Any]]) -> tuple[str, str]:
    now_text = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    subject = f"{city_name}未来3天天气预报 {datetime.now(TZ).strftime('%Y-%m-%d')}"
    lines = [
        f"发送时间：{now_text}（北京时间）",
        f"城市：{country} {adm1} {city_name}",
        "",
    ]
    for item in days:
        lines.extend(
            [
                f"日期：{item.get('fxDate', '-')}",
                f"白天天气：{item.get('textDay', '-')}",
                f"夜间天气：{item.get('textNight', '-')}",
                f"温度：{item.get('tempMin', '-')} ~ {item.get('tempMax', '-')}°C",
                f"风向/风力：{item.get('windDirDay', '-')} / {item.get('windScaleDay', '-')}",
                f"降水概率：{item.get('precip', '-')}",
                f"湿度：{item.get('humidity', '-')}",
                "-" * 24,
            ]
        )
    return subject, "\n".join(lines).rstrip()


def send_mail(subject: str, body: str) -> None:
    smtp_host = require_env("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = require_env("SMTP_USER")
    smtp_password = require_env("SMTP_PASSWORD")
    email_from = require_env("EMAIL_FROM")
    email_to = require_env("EMAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(body)

    context = ssl.create_default_context()
    print(f"准备连接 SMTP: {smtp_host}:{smtp_port}")
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=30) as server:
        server.ehlo()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
    print("邮件发送完成")


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    load_env_file(base_dir / ".env")

    api_host = require_env("QW_API_HOST")
    project_id = require_env("QW_PROJECT_ID")
    credential_id = require_env("QW_CREDENTIAL_ID")
    private_key_path = Path(require_env("QW_PRIVATE_KEY_PATH"))
    location_query = require_env("QW_LOCATION_QUERY")
    lang = os.environ.get("QW_LANG", "zh").strip() or "zh"
    unit = os.environ.get("QW_UNIT", "m").strip() or "m"

    if not private_key_path.is_absolute():
        private_key_path = base_dir / private_key_path
    private_key = private_key_path.read_text(encoding="utf-8")

    print("开始生成 JWT")
    token = build_jwt_token(project_id, credential_id, private_key)
    print(f"开始查询城市: {location_query}")
    location = lookup_location(api_host, token, location_query, lang)
    print(f"城市查询完成: {location.get('name', location_query)} / {location.get('id', '-')}")
    daily = fetch_forecast(api_host, token, location["id"], lang, unit)
    print("天气查询完成，开始组织邮件")
    subject, body = format_mail(
        city_name=location.get("name", location_query),
        adm1=location.get("adm1", ""),
        country=location.get("country", ""),
        days=daily,
    )
    send_mail(subject, body)


if __name__ == "__main__":
    main()
