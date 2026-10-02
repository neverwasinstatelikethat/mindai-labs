"""Предложения самомодификации: ЛЛМ предлагает, эксперт решает, политика ограничена.

Потолок активной политики
-------------------------
``active_policy`` конкатенровала все принятые правки без ограничения длины: при
десятках принятых предложений промпт рос до размеров, которые ``CONTEXT_TOKEN_BUDGET``
и ``AGENT_MAX_*`` всё равно срезают — молча и в другом месте. Потолок
``MAX_ACTIVE_POLICY_CHARS`` стоит на источнике (здесь), а невышедшие правки
перечислены в ``ActivePolicyReport.truncated``: вызывающий слой обязан показать их
как деградацию, а не делать вид, что вся принятая политика действует.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from scientific_tangle.domain.contracts import (
    EvolutionDraft,
    EvolutionProposal,
    FeedbackRequest,
)
from scientific_tangle.services.context_budget import to_prompt_json
from scientific_tangle.services.provider import ModelProvider

EVOLUTION_SYSTEM = """Ты Self-Evolve Agent платформы MindAI.
По экспертной обратной связи предложи ровно одно безопасное улучшение: prompt, rule, alias
или gold_case. Не изменяй trusted graph и production prompt напрямую. Сформулируй проверяемое
изменение и перечисли затронутые regression areas.
"""

# Разделитель блоков политики: два слипшихся предложения читаются моделью как одно.
POLICY_SEPARATOR = "\n\n"
# Потолок длины активной политики в символах. Ограничитель на источнике, а не у
# места вставки: промпт обязан помещаться в бюджет контекста до того, как его
# начнут усекать в рабочем процессе.
MAX_ACTIVE_POLICY_CHARS = 4_000
# Видов предложений, которые действительно куда-то вставляются. ``alias`` и
# ``gold_case`` принятым статусом обещают применение, которого в контуре нет:
# alias живёт рёбрами ``ALIAS_OF`` в графе (их правит разбиратель сущностей), а
# gold_case — файлом корпуса вне Git. Молча выдавать их за действующие нельзя.
APPLIED_KINDS = frozenset({"prompt", "rule"})
UNAPPLIED_KINDS = frozenset({"alias", "gold_case"})


@dataclass(frozen=True, slots=True)
class ActivePolicyReport:
    """Активная политика промпта плюс честный учёт того, что в неё не вошло.

    ``truncated`` и ``not_applied`` — идентификаторы предложений: их достаточно,
    чтобы интерфейс показал деградацию и «принято, но не применено» вместо
    воображаемого «всё принятое действует».
    """

    text: str
    applied: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    not_applied: list[str] = field(default_factory=list)
    limit_chars: int = MAX_ACTIVE_POLICY_CHARS

    @property
    def degraded(self) -> bool:
        return bool(self.truncated)

    def degradation_reasons(self) -> list[str]:
        """Короткие код-причины для ``AnswerPayload.degradation_reasons``/журнала."""
        reasons: list[str] = []
        if self.truncated:
            reasons.append("policy_truncated")
        if self.not_applied:
            reasons.append("proposal_not_applied")
        return reasons

    def notes(self) -> list[str]:
        """Развёрнутые причины — тем же текстом, что и в ``AnswerAssessment.skips``."""
        notes: list[str] = []
        if self.truncated:
            notes.append(
                f"Активная политика усечена до {self.limit_chars} символов: не вошли "
                f"{len(self.truncated)} принятых правок (id: {', '.join(self.truncated[:5])}"
                f"{' …' if len(self.truncated) > 5 else ''})."
            )
        if self.not_applied:
            notes.append(
                f"Принято, но не применено ничем: {len(self.not_applied)} предложений вида "
                "alias/gold_case (id: "
                f"{', '.join(self.not_applied[:5])}{' …' if len(self.not_applied) > 5 else ''})."
            )
        return notes


class EvolutionService:
    """LLM формирует proposal, человек решает, публиковать ли его.

    Хранилище ограничено и упорядочено детерминированно: proposal'ов на каждую
    экспертную реплику хватает на месяцы работы, а активная политика из них
    собирается в порядке, не зависящем от того, в каком порядке их заносили.
    """

    def __init__(
        self,
        provider: ModelProvider,
        capacity: int = 500,
        policy_limit_chars: int = MAX_ACTIVE_POLICY_CHARS,
    ) -> None:
        self._provider = provider
        self._proposals: dict[UUID, EvolutionProposal] = {}
        self._capacity = capacity
        self._policy_limit_chars = policy_limit_chars

    def _trim(self) -> None:
        # Выгружаем по ключу хранения, а не по id объекта: вызывающий код волен
        # положить запись под другим ключом, и pop по item.id тогда промахнётся.
        # Принятые предложения участвуют в активной политике промпта, поэтому
        # первыми уходят отклонённые и ещё не рассмотренные.
        while len(self._proposals) > self._capacity:
            key, _ = min(
                self._proposals.items(),
                key=lambda entry: (entry[1].status == "accepted", entry[1].created_at),
            )
            self._proposals.pop(key)

    async def propose(self, feedback: FeedbackRequest) -> EvolutionProposal:
        draft = await self._provider.complete_model(
            EVOLUTION_SYSTEM,
            to_prompt_json(feedback),
            EvolutionDraft,
        )
        proposal = EvolutionProposal(
            source_query_id=feedback.query_id,
            kind=draft.kind,
            title=draft.title,
            change=draft.change,
            impact=draft.impact,
        )
        self._proposals[proposal.id] = proposal
        self._trim()
        return proposal

    def review(self, proposal_id: UUID, accepted: bool) -> EvolutionProposal:
        """Меняет статус предложения.

        A/B-ворота проверяет вызывающий слой по серверному состоянию
        (`services/durable_state.py`, таблица `nk_experiments`): эксперименты
        перестали быть словарём процесса, и вороти по локальной копии
        навсегда заперли бы принятие — ни один прогноз не был бы «пройден».

        Статус ``accepted`` обещает применение только видам ``prompt``/``rule``:
        ``alias`` и ``gold_case`` ничем не применяются в этом контуре, и это
        состояние видно в ``active_policy_report().not_applied``, а не прячется в
        статусе (контракт ``EvolutionProposal.status`` третьего значения не имеет).
        """
        current = self._proposals[proposal_id]
        updated = current.model_copy(update={"status": "accepted" if accepted else "rejected"})
        self._proposals[proposal_id] = updated
        return updated

    def restore(self, proposal: EvolutionProposal) -> None:
        """Восстанавливает зеркало из серверного состояния при подъёме сервиса.

        Хранилище — `services/durable_state.py` (`nk_evolution_proposals`), а этот
        словарь нужен только чтобы посчитать активную политику промпта; молча
        потерять принятые предложения после рестарта — значит изменить поведение
        агента без всякой причины.
        """
        self._proposals[proposal.id] = proposal
        self._trim()

    def accepted_proposals(self) -> list[EvolutionProposal]:
        """Принятые предложения в детерминированном порядке (по created_at, затем id)."""
        return sorted(
            (proposal for proposal in self._proposals.values() if proposal.status == "accepted"),
            key=lambda proposal: (proposal.created_at, str(proposal.id)),
        )

    def active_policy_report(self) -> ActivePolicyReport:
        """Текст активной политики плюс учёт не вошедших и неприменяемых правок.

        Порядок вставки dict'ом сохранялся, поэтому две идентичные очереди принятых
        предложений давали разный системный промпт; отбор идёт по длине, а не по
        «сколько влезло в чужой бюджет», и всё срезанное остаётся названным.
        """
        accepted = self.accepted_proposals()
        applied: list[str] = []
        truncated: list[str] = []
        blocks: list[str] = []
        used = 0
        for proposal in accepted:
            if proposal.kind not in APPLIED_KINDS:
                continue
            block = proposal.change.strip()
            if not block:
                continue
            # Первый блок берём всегда: пустая политика при непустом множестве
            # принятых правок означала бы «агент ничего не знает про решения
            # эксперта» вместо «влезло только начало».
            extra = len(block) + (len(POLICY_SEPARATOR) if blocks else 0)
            if blocks and used + extra > self._policy_limit_chars:
                truncated.append(str(proposal.id))
                continue
            blocks.append(block)
            applied.append(str(proposal.id))
            used += extra
        not_applied = [
            str(proposal.id)
            for proposal in accepted
            if proposal.kind in UNAPPLIED_KINDS
        ]
        return ActivePolicyReport(
            text=POLICY_SEPARATOR.join(blocks),
            applied=applied,
            truncated=truncated,
            not_applied=not_applied,
            limit_chars=self._policy_limit_chars,
        )

    def active_policy(self) -> str:
        """Текст активной политики (обратно совместимый контракт для промпта).

        Всё, что не влезло или не применяется, остаётся в
        ``active_policy_report()``: вызывающий слой обязан показывать деградацию
        оттуда, а не полагать, что короткая строка и есть полное состояние.
        """
        return self.active_policy_report().text
