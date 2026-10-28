"""Spanish (Argentina) replies: opt-outs, escalations and the opt-out line."""

import pytest

from harvey.agents.handler import ESCALATION_PATTERNS, _no_accents, _flat, _opt_out_reason
from harvey.config import ComplianceConfig
from harvey.gate import BANNED_PHRASES


@pytest.mark.parametrize("reply", [
    "No, gracias.",
    "no gracias",
    "No muchas gracias",
    "Por favor denme de baja.",
    "Dejá de escribirme, gracias.",
    "Les pido que no me vuelvan a escribir",
    "Sáquenme de su lista por favor",
    "No quiero recibir más mails de ustedes",
    "Borren mis datos",
    "baja",
])
def test_spanish_opt_outs_are_honoured(reply):
    assert _opt_out_reason(reply, reply) is not None


@pytest.mark.parametrize("reply", [
    "Hola, ¿me sacás de una duda? ¿Hacen estuches con ventana?",
    "Sacame de una duda: ¿cuál es el mínimo de producción?",
    "Sí o no, gracias por avisar.",
    "No gracias a ustedes, todavía estamos evaluando proveedores.",
    "Tenemos mucha demanda en diciembre, ¿llegan con las cajas?",
    "Me interesa, ¿me pasás un presupuesto?",
])
def test_ordinary_spanish_replies_are_not_opt_outs(reply):
    assert _opt_out_reason(reply, reply) is None


def _escalates(text):
    flat = _no_accents(_flat(text))
    return any(p in flat for p in ESCALATION_PATTERNS)


def test_spanish_legal_threats_escalate():
    assert _escalates("Si me vuelven a escribir les mando una carta documento.")
    assert _escalates("Voy a hacer la denuncia ante la AAIP.")
    assert _escalates("Esto viola la Ley de Protección de Datos Personales.")


def test_market_demand_does_not_escalate():
    assert not _escalates("La demanda de packaging creció mucho este año.")


def test_spanish_opt_out_line_is_accepted():
    line = '¿No te interesa? Respondé "no, gracias" y no te escribo más.'
    assert ComplianceConfig(opt_out_line=line).opt_out_line == line


def test_spanish_opt_out_line_passes_the_gate():
    line = '¿No te interesa? Respondé "no, gracias" y no te escribo más.'.lower()
    assert not [p for p in BANNED_PHRASES if p in line]
