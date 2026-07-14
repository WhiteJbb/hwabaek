"""에이전트 런타임 (Native Agent Runtime) — 배치 소비 LLM 툴 루프.

Plan 코어 의미론 §1(배치 소비)·§2(이력 표현·절단)를 구현한다. 이 모듈은
세션·합의 로직과 결합하지 않는다 (D-015) — 세션과의 상호작용은 전부
SessionCommands 프로토콜을 통해서만 하며, 프로바이더 SDK 타입을 모른다
(llm/base 계약만 사용).

도구 호출 검증(§17)은 SessionCommands 구현(SessionManager)이 수행하고,
위반은 ToolError로 돌려받아 구조화된 tool error로 모델에 반환한다. 열린 토론은
max_turns로 막고, proposal/voting/revision은 단계별 2회 상한으로 별도 제한한다(D-033).
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from hwabaek.contracts import (
    AgentState,
    ContractError,
    Message,
    MessageType,
    Usage,
)
from hwabaek.llm.base import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMRuntimeError,
    Role,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)

# 에이전트에게 부여하는 도구 3종 — 이름은 contracts.COMMAND_*와 일치해야 한다.
AGENT_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="send_message",
        description=(
            "Send a message to teammates. Use recipients=[\"*\"] to broadcast to "
            "everyone else, or list specific agent names."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Agent names, or [\"*\"] for broadcast.",
                },
                "content": {"type": "string"},
            },
            "required": ["recipients", "content"],
        },
    ),
    ToolSpec(
        name="submit_result",
        description=(
            "Submit the final deliverable as a result proposal. Only allowed while "
            "the session is running; teammates will vote on it."
        ),
        input_schema={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    ),
    ToolSpec(
        name="vote_result",
        description=(
            "Vote on the active result proposal. Omit proposal_id to vote on the "
            "current active proposal. Reject requires a reason so the proposer "
            "can revise."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "description": "Optional; defaults to the active proposal.",
                },
                "decision": {"type": "string", "enum": ["approve", "reject"]},
                "reason": {"type": "string"},
            },
            "required": ["decision"],
        },
    ),
)


class ToolError(Exception):
    """도구 호출 검증 실패 — 모델이 수정할 수 있도록 is_error tool result로 반환된다."""


class SessionCommands(Protocol):
    """에이전트가 세션에 요청할 수 있는 명령 — SessionManager가 구현한다.

    각 메서드는 상태별 허용 규칙(§17)을 검증하고, 위반 시 ToolError를 던진다.
    반환 문자열은 모델에게 보여줄 도구 실행 결과다 (영어 ASCII).
    """

    def send_message(self, sender: str, recipients: list[str], content: str) -> str: ...

    def submit_result(self, sender: str, content: str) -> str: ...

    def vote_result(
        self, sender: str, proposal_id: str, decision: str, reason: str
    ) -> str: ...


class AgentStateHooks(Protocol):
    """에이전트 상태·사용량 통지 — SessionManager가 구현한다."""

    def on_state(self, agent: str, state: AgentState, detail: str | None = None) -> None: ...

    def on_usage(self, agent: str, usage: Usage) -> None: ...

    def on_fatal_error(self, agent: str, error: LLMError) -> None: ...

    def on_exhausted(self, agent: str) -> None: ...

    async def before_call(self, agent: str) -> bool: ...

    async def after_call(self, agent: str, usage: Usage) -> None: ...

    async def on_call_released(self, agent: str) -> None: ...

    def tools_for(self, agent: str) -> frozenset[str]: ...

    def instruction_for(self, agent: str) -> str | None: ...

    def retry_instruction(self, agent: str) -> str | None: ...

    def preserve_after_turn_limit(self, agent: str) -> bool: ...

    def allow_call_after_turn_limit(self, agent: str) -> bool: ...


# 이력 절단 기본값 — 에이전트당 유지할 최대 턴 수 (시스템 프롬프트 별도).
DEFAULT_HISTORY_LIMIT = 60

_TRUNCATION_NOTICE = (
    "[system notice] Older conversation turns were truncated to fit the context "
    "window. The original task above is preserved."
)


def merge_batch(messages: list[Message]) -> str:
    """수신 배치를 발신자 태깅으로 병합해 하나의 user 턴 본문을 만든다 (§2).

    타입별 렌더링: 제안·투표를 일반 채팅과 구별되게 표기한다 — 실 스모크에서
    제안이 채팅과 동일하게 보여 심의자들이 vote_result 대신 채팅으로만 동의를
    표하다 전원 기권(no_quorum) 처리된 것에 대한 대응. 제안에는 즉시 투표하라는
    행동 지시를 함께 싣는다(투표 권한이 없는 수신자를 위해 조건부 문구).
    """
    parts = []
    for message in messages:
        if message.type is MessageType.RESULT_PROPOSAL:
            parts.append(
                f"[result proposal from {message.sender}] "
                f"(proposal_id: {message.proposal_id})\n{message.content}\n"
                "[action required] The session is now VOTING on the proposal "
                "above. If you have the vote_result tool, cast your vote NOW "
                "(approve, or reject with a concrete reason); you may omit "
                "proposal_id to vote on this active proposal. General chat is "
                "closed during voting. One corrective call is allowed; a member "
                "that still does not vote is counted as abstaining."
            )
        elif message.type is MessageType.VOTE:
            decision = message.vote.value if message.vote else "vote"
            reason = f"\n{message.content}" if message.content else ""
            parts.append(f"[vote from {message.sender}: {decision}]{reason}")
        else:
            parts.append(f"[from: {message.sender}]\n{message.content}")
    return "\n\n".join(parts)


def truncate_history(turns: list[Turn], limit: int) -> list[Turn]:
    """이력 상한 초과 시 절단 (§2 보존 우선순위의 M2a 최소 구현).

    첫 user 턴(원본 태스크)은 보존하고, 최근 턴을 우선 유지하며, 절단 사실을
    명시 턴으로 삽입한다. 제안·투표 원문 보호의 세밀한 규칙은 M5 compaction에서.
    assistant tool_calls 턴과 그 tool_results user 턴이 갈라지지 않도록 경계를
    user 턴 시작점으로 맞춘다.
    """
    if len(turns) <= limit:
        return turns
    head = turns[:1]
    tail = turns[-(limit - 2):]
    # tool_results로 시작하면 대응하는 tool_calls가 잘린 것 — 경계를 뒤로 민다.
    while tail and tail[0].role is Role.USER and tail[0].tool_results:
        tail = tail[1:]
    notice = Turn(role=Role.USER, content=_TRUNCATION_NOTICE)
    return head + [notice] + tail


class AgentLoop:
    """에이전트 1개의 실행 루프.

    수명주기: 초기 태스크 턴으로 1회 호출 → (인박스 대기 → 배치 병합 →
    LLM 호출 → tool_use 처리 반복) → 세션 종료. 열린 토론 max_turns 뒤에는
    대기하며, 결정 자격이 있으면 유한한 hard phase 호출만 추가 수행한다.
    """

    def __init__(
        self,
        name: str,
        system_prompt: str,
        model: str,
        task: str,
        *,
        llm: LLMClient,
        bus,  # MessageBus — wait_for_messages/drain만 사용
        commands: SessionCommands,
        hooks: AgentStateHooks,
        max_turns: int,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self.name = name
        self._system_prompt = system_prompt
        self._model = model
        self._task = task
        self._llm = llm
        self._bus = bus
        self._commands = commands
        self._hooks = hooks
        self._max_turns = max_turns
        self._history_limit = history_limit
        self._turns: list[Turn] = []
        self._calls_made = 0
        self._dead = False

    async def run(self) -> None:
        """루프 본체. 세션 종료 시 SessionManager가 태스크를 취소한다."""
        first = Turn(
            role=Role.USER,
            content=(
                f"[task]\n{self._task}\n\n"
                "Collaborate with your teammates using the tools. "
                "Respond in the language of the task."
            ),
        )
        self._turns.append(first)
        await self._think_and_act()

        # dead면 즉시 종료 — 루프를 계속 돌면 IDLE 보고가 DEAD 상태를 덮어써
        # 생존자 계산이 틀어진다 (failed(agent_error)가 failed(idle)로 오분류).
        while not self._dead:
            if self._calls_made >= self._max_turns:
                preserve = getattr(
                    self._hooks, "preserve_after_turn_limit", None
                )
                if preserve is None or not preserve(self.name):
                    self._hooks.on_state(
                        self.name, AgentState.IDLE, detail="max_turns exhausted"
                    )
                    on_exhausted = getattr(self._hooks, "on_exhausted", None)
                    if on_exhausted is not None:
                        on_exhausted(self.name)
                    return
                self._hooks.on_state(
                    self.name,
                    AgentState.IDLE,
                    detail="max_turns reached; waiting for decision phase",
                )
            else:
                self._hooks.on_state(self.name, AgentState.IDLE)
            await self._bus.wait_for_messages(self.name)
            batch = self._bus.drain(self.name)
            drain_notices = getattr(self._bus, "drain_notices", None)
            notices = drain_notices(self.name) if drain_notices else []
            if not batch and not notices:
                continue
            if batch:
                self._turns.append(Turn(role=Role.USER, content=merge_batch(batch)))
            for notice in notices:
                self._turns.append(Turn(role=Role.USER, content=notice))
            await self._think_and_act()

    async def _think_and_act(self) -> None:
        """LLM 호출 1회 + 후속 tool_use 체인 처리 (체인도 호출 수에 포함)."""
        while True:
            if self._calls_made >= self._max_turns:
                allow_extra = getattr(
                    self._hooks, "allow_call_after_turn_limit", None
                )
                if allow_extra is None or not allow_extra(self.name):
                    return
            before_call = getattr(self._hooks, "before_call", None)
            if before_call is not None and not await before_call(self.name):
                return
            self._hooks.on_state(self.name, AgentState.THINKING)
            self._turns = truncate_history(self._turns, self._history_limit)
            instruction_for = getattr(self._hooks, "instruction_for", None)
            instruction = instruction_for(self.name) if instruction_for else None
            request_turns = tuple(self._turns)
            if instruction:
                request_turns += (Turn(role=Role.USER, content=instruction),)
            tools_for = getattr(self._hooks, "tools_for", None)
            allowed_tools = tools_for(self.name) if tools_for else None
            tools = (
                tuple(tool for tool in AGENT_TOOLS if tool.name in allowed_tools)
                if allowed_tools is not None
                else AGENT_TOOLS
            )
            request = LLMRequest(
                model=self._model,
                system_prompt=self._system_prompt,
                turns=request_turns,
                tools=tools,
                cache_system_prefix=True,
            )
            response: LLMResponse | None = None
            try:
                response = await self._llm.complete(request)
            except LLMError as error:
                # SDK 자체 재시도가 소진된 뒤 도달한다 — 세션에 귀책과 함께 보고하고
                # 루프를 완전히 끝낸다(dead 에이전트는 인박스 소비도 중단).
                self._dead = True
                self._hooks.on_fatal_error(self.name, error)
                return
            except Exception:
                self._dead = True
                self._hooks.on_fatal_error(
                    self.name,
                    LLMRuntimeError("unexpected LLM client error"),
                )
                return
            finally:
                if response is None:
                    release = getattr(self._hooks, "on_call_released", None)
                    if release is not None:
                        await release(self.name)
            self._calls_made += 1
            after_call = getattr(self._hooks, "after_call", None)
            try:
                if after_call is not None:
                    await after_call(self.name, response.usage)
                else:
                    self._hooks.on_usage(self.name, response.usage)
            except BaseException:
                release = getattr(self._hooks, "on_call_released", None)
                if release is not None:
                    await asyncio.shield(release(self.name))
                raise
            self._turns.append(
                Turn(
                    role=Role.ASSISTANT,
                    content=response.text,
                    tool_calls=response.tool_calls,
                )
            )
            if response.stop is not StopReason.TOOL_USE:
                retry_instruction = getattr(self._hooks, "retry_instruction", None)
                retry = retry_instruction(self.name) if retry_instruction else None
                if retry:
                    self._turns.append(Turn(role=Role.USER, content=retry))
                    continue
                return
            offered_tools = frozenset(tool.name for tool in tools)
            results_list: list[ToolResult] = []
            sent_message = False
            for call in response.tool_calls:
                if call.name == "send_message" and sent_message:
                    results_list.append(self._tool_error_result(
                        call,
                        ToolError(
                            "send_message rejected: only one call is allowed per "
                            "response; include all targets in recipients"
                        ),
                    ))
                    continue
                if call.name == "send_message":
                    sent_message = True
                results_list.append(self._execute_tool(call, offered_tools))
            results = tuple(results_list)
            # 병렬 tool_use여도 모든 결과를 하나의 user 턴으로 반환한다.
            self._turns.append(Turn(role=Role.USER, tool_results=results))

    def _execute_tool(
        self, call: ToolCall, offered_tools: frozenset[str]
    ) -> ToolResult:
        """도구 1건 실행 — 검증 실패는 구조화된 tool error로 반환한다 (§17)."""
        try:
            if call.name not in offered_tools:
                raise ToolError(
                    f"tool {call.name!r} was not offered for this call"
                )
            output = self._dispatch(call)
            return ToolResult(tool_call_id=call.id, content=output)
        except (ToolError, ContractError) as error:
            return self._tool_error_result(call, error)

    def _tool_error_result(
        self, call: ToolCall, error: ToolError | ContractError
    ) -> ToolResult:
        """도구 오류를 모델 응답과 관측 이벤트에 같은 형태로 남긴다."""
        # 모델에게만 오류를 돌려주면 실 세션 디버깅이 불가능하므로 상태 detail에도
        # 남긴다. 사용자 노출 detail은 영어 ASCII로 제한한다.
        self._hooks.on_state(
            self.name,
            AgentState.THINKING,
            detail=f"tool error [{call.name}]: {str(error)[:120]}",
        )
        return ToolResult(tool_call_id=call.id, content=str(error), is_error=True)

    def _dispatch(self, call: ToolCall) -> str:
        args = call.arguments
        if call.name == "send_message":
            recipients = args.get("recipients")
            content = args.get("content", "")
            if not isinstance(recipients, list) or not recipients:
                raise ToolError("recipients must be a non-empty list of agent names")
            return self._commands.send_message(self.name, recipients, content)
        if call.name == "submit_result":
            return self._commands.submit_result(self.name, args.get("content", ""))
        if call.name == "vote_result":
            return self._commands.vote_result(
                self.name,
                args.get("proposal_id", ""),
                args.get("decision", ""),
                args.get("reason", ""),
            )
        raise ToolError(
            f"unknown tool {call.name!r}; available: send_message, submit_result, "
            "vote_result"
        )
