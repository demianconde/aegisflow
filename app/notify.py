"""Notificações por e-mail via SMTP — best-effort: nunca quebra o fluxo principal.

Se as variáveis SMTP não estiverem configuradas, as funções viram no-op (apenas
registram um log). O envio roda numa thread para não bloquear o event loop.
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

from app.config import get_settings
from app.logging_config import get_logger

_log = get_logger("notify")


def _send_sync(
    host: str, port: int, user: str | None, password: str | None, msg: EmailMessage
) -> None:
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)


async def send_signup_notification(email: str, tenant_name: str) -> None:
    """Avisa o dono quando alguém ativa o teste grátis. No-op se SMTP não configurado."""
    s = get_settings()
    to_addr = s.leads_notify_email or s.smtp_from or s.smtp_user
    if not (s.smtp_host and to_addr):
        _log.info("signup_notify_skipped", reason="smtp_nao_configurado")
        return

    sender = s.smtp_from or s.smtp_user or to_addr
    msg = EmailMessage()
    msg["Subject"] = f"🎉 Novo cadastro grátis no AegisFlow: {email or 'sem e-mail'}"
    msg["From"] = sender
    msg["To"] = to_addr
    if email:
        msg["Reply-To"] = email
    msg.set_content(
        "\n".join(
            [
                "Alguém ativou o teste grátis do AegisFlow:",
                "",
                f"E-mail: {email or '-'}",
                f"Conta:  {tenant_name or '-'}",
                "",
                "Console do dono: https://aegisflow.tech/gestaoaegis",
            ]
        )
    )

    try:
        await asyncio.to_thread(
            _send_sync, s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, msg
        )
        _log.info("signup_notify_sent", to=to_addr)
    except Exception as exc:  # noqa: BLE001
        _log.warning("signup_notify_failed", error=str(exc))


async def send_lead_notification(lead: dict) -> None:
    """Envia um e-mail avisando de um novo lead. No-op se SMTP não configurado."""
    s = get_settings()
    to_addr = s.leads_notify_email or s.smtp_from or s.smtp_user
    if not (s.smtp_host and to_addr):
        _log.info("lead_notify_skipped", reason="smtp_nao_configurado")
        return

    sender = s.smtp_from or s.smtp_user or to_addr
    msg = EmailMessage()
    msg["Subject"] = f"🎯 Novo lead: {lead.get('name') or 'sem nome'}"
    msg["From"] = sender
    msg["To"] = to_addr
    if lead.get("email"):
        msg["Reply-To"] = lead["email"]
    corpo = [
        "Novo lead no formulário de interesse do AegisFlow:",
        "",
        f"Nome:         {lead.get('name') or '-'}",
        f"E-mail:       {lead.get('email') or '-'}",
        f"Empresa:      {lead.get('company') or '-'}",
        f"Gasto mensal: {lead.get('monthly_spend') or '-'}",
        f"Mensagem:     {lead.get('message') or '-'}",
        f"Origem:       {lead.get('source') or '-'}",
        "",
        "Ver todos os leads: https://aegisflow.tech/gestaoaegis",
    ]
    msg.set_content("\n".join(corpo))

    try:
        await asyncio.to_thread(
            _send_sync, s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, msg
        )
        _log.info("lead_notify_sent", to=to_addr)
    except Exception as exc:  # noqa: BLE001
        _log.warning("lead_notify_failed", error=str(exc))
