const view = document.querySelector("#route-view");
const nav = document.querySelector("#primary-nav");
const connection = document.querySelector("#connection-status");
const toastRegion = document.querySelector("#toast-region");
const liveRegion = document.querySelector("#live-region");

const TERMINAL = new Set(["completed", "failed", "cancelled"]);
const EVENT_TYPES = [
  "session_status",
  "message",
  "agent_state",
  "usage",
  "vote_status",
  "result",
];
const HASH_ROUTES = Object.freeze({ home: "#/", sessions: "#/sessions", teams: "#/teams" });

let activeStream = null;
let activeElapsedTimer = null;
let routeVersion = 0;

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail || "";
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function tokenTotals(usage = {}) {
  const work = ["input_tokens", "output_tokens", "cache_write_tokens"]
    .reduce((sum, key) => sum + Number(usage[key] || 0), 0);
  const cacheRead = Number(usage.cache_read_tokens || 0);
  return { work, cacheRead, processed: work + cacheRead };
}

function number(value) {
  return new Intl.NumberFormat("ko-KR").format(Number(value || 0));
}

function dateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function elapsedTime(startedAt, finishedAt = null) {
  const started = new Date(startedAt).getTime();
  const finished = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) return "—";
  const totalSeconds = Math.floor((finished - started) / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours}시간 ${minutes}분`;
  if (minutes) return `${minutes}분 ${seconds}초`;
  return `${seconds}초`;
}

function statusLabel(status, failReason = null) {
  const labels = {
    running: "논의 중",
    voting: "표결 중",
    completed: "합의 완료",
    failed: "종료됨",
    cancelled: "취소됨",
  };
  const reasons = {
    budget: "예산 초과",
    messages: "메시지 한도",
    idle: "응답 없음",
    agent_error: "에이전트 오류",
    no_quorum: "정족수 미달",
    interrupted: "서버 중단",
  };
  if (status === "failed" && failReason) {
    return `${labels.failed} · ${reasons[failReason] || failReason}`;
  }
  return labels[status] || status || "알 수 없음";
}

function statusBadge(session) {
  const status = session?.status || "failed";
  return `<span class="status status-${escapeHtml(status)}">${escapeHtml(
    statusLabel(status, session?.fail_reason),
  )}</span>`;
}

function failureExplanation(session) {
  const details = {
    "processed token limit reached before next call": "캐시를 포함한 전체 처리 상한에 도달해 다음 호출을 시작하지 않았습니다. 태스크 범위를 줄이거나 Deep 팀을 선택해 다시 시도하세요.",
    "processed token limit exceeded": "응답 정산 결과 캐시를 포함한 전체 처리 상한을 넘었습니다. 태스크 범위를 줄이거나 Deep 팀을 선택해 다시 시도하세요.",
    "work token budget reserved for decision phase": "결론 단계에 필요한 호출 예약량보다 남은 작업 예산이 적어 종료했습니다. 더 짧은 태스크로 나누거나 Deep 팀을 선택하세요.",
    "work token budget exceeded": "응답 정산 결과 작업 토큰 예산을 넘었습니다. 태스크를 나누거나 더 큰 팀 프로필을 선택하세요.",
    "maximum proposal versions rejected": "허용된 수정 횟수 안에 승인을 얻지 못했습니다. 마지막 초안과 반려 사유를 확인해 태스크 조건을 명확히 한 뒤 다시 시도하세요.",
    "no proposer calls remain for decision phase": "결과를 제출할 에이전트의 호출 횟수가 남지 않았습니다. 태스크를 더 작게 나누거나 더 큰 팀 프로필을 선택하세요.",
    "proposer did not submit within decision call limit": "제안 단계의 두 번의 호출 안에 결과가 제출되지 않았습니다. 요구 결과를 더 구체적으로 적어 다시 시도하세요.",
  };
  if (details[session.fail_detail]) return details[session.fail_detail];
  const guidance = {
    budget: "작업 예산 또는 전체 처리 상한에 도달했습니다. 태스크 범위를 줄이거나 더 큰 팀 프로필을 선택하세요.",
    messages: "메시지 상한에 도달했습니다. 태스크 범위를 좁히거나 요구 결과를 더 구체적으로 적어 주세요.",
    idle: "에이전트가 결론을 내리지 못한 채 유휴 상태가 됐습니다. 요구 결과와 제약을 더 구체적으로 적어 다시 시도하세요.",
    no_quorum: "필요한 승인을 얻지 못했습니다. 마지막 초안과 반려 사유를 확인한 뒤 조건을 보완해 다시 시도하세요.",
    agent_error: "모델 호출 중 오류가 발생했습니다. 연결과 인증 상태를 확인한 뒤 다시 시도하세요.",
    interrupted: "서버가 중단되어 세션을 마치지 못했습니다. 서버를 다시 시작한 뒤 재실행하세요.",
  };
  return guidance[session.fail_reason] || session.fail_detail || "세션을 완료하지 못했습니다.";
}

function profileLabel(team) {
  return ({ quick: "Quick", default: "Standard", deep: "Deep" })[team.name] || team.name;
}

function setConnection(state, label) {
  connection.dataset.state = state;
  connection.querySelector("span:last-child").textContent = label;
}

function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = kind === "error" ? "toast-error" : "toast-info";
  item.textContent = message;
  toastRegion.replaceChildren(item);
  window.setTimeout(() => {
    if (item.isConnected) item.remove();
  }, 4200);
}

function announce(message) {
  liveRegion.textContent = "";
  window.requestAnimationFrame(() => { liveRegion.textContent = message; });
}

function friendlyError(error) {
  if (!(error instanceof ApiError)) return "서버에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요.";
  if (error.status === 404) return "요청한 기록을 찾지 못했습니다.";
  if (error.status === 409) return "이미 진행 중인 회의가 있거나 종료된 회의입니다.";
  if (error.status === 422) return "입력 내용을 확인해 주세요. 태스크는 비워 둘 수 없습니다.";
  if (error.status === 400) return "선택한 팀이나 요청 형식을 확인해 주세요.";
  return "요청을 처리하지 못했습니다. 잠시 뒤 다시 시도해 주세요.";
}

async function api(path, options = {}) {
  try {
    const response = await fetch(path, {
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new ApiError(response.status, body?.detail || "");
    setConnection("connected", "서버 연결됨");
    return body;
  } catch (error) {
    if (!(error instanceof ApiError)) setConnection("disconnected", "서버 연결 끊김");
    throw error;
  }
}

function closeStream() {
  if (activeStream) activeStream.close();
  activeStream = null;
  if (activeElapsedTimer) window.clearInterval(activeElapsedTimer);
  activeElapsedTimer = null;
}

function routePath() {
  const raw = window.location.hash.slice(1) || "/";
  return raw.startsWith("/") ? raw : `/${raw}`;
}

function updateNavigation(path) {
  for (const link of nav.querySelectorAll("a")) {
    const target = link.getAttribute("href").slice(1);
    const current = target === "/"
      ? path === "/"
      : path === target || path.startsWith(`${target}/`);
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function loading(title = "회의 기록을 펼치는 중입니다") {
  view.innerHTML = `
    <section class="page" aria-busy="true">
      <header class="page-header"><div><p class="eyebrow">잠시만요</p><h1>${escapeHtml(title)}</h1></div></header>
      <div class="panel"><div class="skeleton skeleton-title"></div><div class="skeleton"></div><div class="skeleton skeleton-short"></div></div>
    </section>`;
}

function errorPage(error, retry) {
  view.innerHTML = `
    <section class="page">
      <header class="page-header"><div><p class="eyebrow">연결 안내</p><h1>화백을 열지 못했습니다</h1></div></header>
      <div class="notice notice-error" role="alert">
        <strong>${escapeHtml(friendlyError(error))}</strong>
        <button class="button button-quiet" id="retry-button" type="button">다시 시도</button>
      </div>
    </section>`;
  document.querySelector("#retry-button")?.addEventListener("click", retry);
}

function budgetPhaseLabel(phase, work, termination = {}) {
  const labels = {
    discussion: "토론 단계",
    synthesis: "종합 단계",
    proposal: "제안 단계",
    voting: "표결 단계",
    revision: "수정 단계",
  };
  if (phase) return labels[phase] || String(phase);
  if (termination.proposal_by && work >= termination.proposal_by) return labels.proposal;
  if (termination.synthesis_at && work >= termination.synthesis_at) return labels.synthesis;
  if (termination.synthesis_at || termination.proposal_by) return labels.discussion;
  return null;
}

function usageMarkup(
  usage,
  budget = null,
  processedLimit = null,
  phase = null,
  termination = {},
) {
  const totals = tokenTotals(usage);
  const percent = budget ? Math.min(100, Math.round((totals.work / budget) * 100)) : 0;
  const phaseLabel = budgetPhaseLabel(phase, totals.work, termination);
  return `
    <div class="usage-summary">
      <div class="session-meta"><span>작업 ${number(totals.work)}</span><span>캐시 재사용 ${number(totals.cacheRead)}</span><span>전체 처리 ${number(totals.processed)}</span>${budget ? `<span>작업 예산 ${number(budget)}</span>` : ""}${processedLimit ? `<span>처리 상한 ${number(processedLimit)}</span>` : ""}${phaseLabel ? `<span>현재 ${escapeHtml(phaseLabel)}</span>` : ""}</div>
      ${budget ? `<div class="usage-bar" role="progressbar" aria-label="작업 토큰 예산 사용량" aria-valuemin="0" aria-valuemax="${budget}" aria-valuenow="${Math.min(totals.work, budget)}"><span class="usage-fill" style="width:${percent}%"></span></div>` : ""}
    </div>`;
}

function sessionCard(session) {
  return `
    <a class="session-card" href="#/sessions/${encodeURIComponent(session.id)}">
      <div class="timeline-head">${statusBadge(session)}<time>${escapeHtml(dateTime(session.created_at))}</time></div>
      <h3>${escapeHtml(session.task)}</h3>
      <div class="session-meta"><span>팀 ${escapeHtml(session.team_name)}</span><span>작업 ${number(tokenTotals(session.usage).work)}</span></div>
    </a>`;
}

function empty(message, action = "") {
  return `<div class="empty-state"><p>${escapeHtml(message)}</p>${action}</div>`;
}

async function renderHome(version) {
  loading("새 화백 회의를 준비하는 중입니다");
  try {
    const [teamData, sessionData] = await Promise.all([api("/teams"), api("/sessions?limit=5")]);
    if (version !== routeVersion) return;
    const teams = teamData.teams || [];
    const sessions = sessionData.sessions || [];
    const active = sessions.find((session) => session.status === "running" || session.status === "voting");
    const selectedTeam = teamData.default_team || "default";
    const defaultTeamExists = teams.some((team) => team.name === selectedTeam);
    const teamOptions = teams.map((team) => {
      const work = team.termination?.token_budget;
      const agents = team.agents?.length || 0;
      return `<option value="${escapeHtml(team.name)}"${team.name === selectedTeam ? " selected" : ""}>${escapeHtml(profileLabel(team))} · 작업 ${number(work)} · ${number(agents)}인 — ${escapeHtml(team.description || team.default_model)}</option>`;
    }).join("");
    view.innerHTML = `
      <section class="page">
        <header class="page-header hero-header">
          <div><p class="eyebrow">자율 협업 · 합의 기록</p><h1>서로 다른 관점이<br>하나의 결론에 닿도록</h1><p class="page-intro">태스크를 올리면 대등들이 토론하고, 제안하고, 표결합니다. 과정과 결과는 이곳에 남습니다.</p></div>
        </header>
        ${active ? `<div class="notice notice-active"><strong>진행 중인 회의가 있습니다.</strong><p>${escapeHtml(active.task)}</p><a class="button button-quiet" href="#/sessions/${encodeURIComponent(active.id)}">회의로 돌아가기</a></div>` : ""}
        <div class="dashboard-grid">
          <section class="panel" aria-labelledby="new-session-title">
            <p class="eyebrow">새 의제</p><h2 id="new-session-title">화백에 부치기</h2>
            ${defaultTeamExists ? "" : `<div class="notice notice-error" role="alert"><strong>서버 기본 팀을 찾을 수 없습니다.</strong><p>서버를 올바른 --team 값으로 다시 시작해 주세요.</p></div>`}
            <form id="session-form" class="form-stack">
              <label class="field"><span>논의할 태스크</span><textarea id="task-input" name="task" rows="7" maxlength="12000" placeholder="예: 신규 서비스 아이디어의 장단점을 검토하고 실행 제안서를 작성해 줘" required ${active || !defaultTeamExists ? "disabled" : ""}></textarea></label>
              <label class="field"><span>참여 팀</span><select id="team-select" name="team" aria-describedby="team-help" ${active || !defaultTeamExists ? "disabled" : ""}>${teamOptions}</select></label>
              <p id="team-help" class="session-meta">Quick은 짧고 가역적인 선택, Standard는 일반 기술 결정, Deep은 고위험·비가역적 결정에 적합합니다.</p>
              <p id="form-error" class="notice notice-error" role="alert" hidden></p>
              <button class="button button-primary" type="submit" ${active || !teams.length || !defaultTeamExists ? "disabled" : ""}>회의 시작</button>
            </form>
          </section>
          <section class="panel" aria-labelledby="recent-title">
            <div class="section-heading"><div><p class="eyebrow">기록</p><h2 id="recent-title">최근 회의</h2></div><a href="#/sessions">전체 보기</a></div>
            <div class="session-list">${sessions.length ? sessions.map(sessionCard).join("") : empty("아직 열린 회의가 없습니다.")}</div>
          </section>
        </div>
      </section>`;

    document.querySelector("#session-form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const task = document.querySelector("#task-input").value.trim();
      const team = document.querySelector("#team-select").value;
      const error = document.querySelector("#form-error");
      if (!task) {
        error.hidden = false;
        error.textContent = "논의할 태스크를 입력해 주세요.";
        document.querySelector("#task-input").focus();
        return;
      }
      const button = event.currentTarget.querySelector("button[type=submit]");
      button.disabled = true;
      button.textContent = "회의를 여는 중…";
      error.hidden = true;
      try {
        const created = await api("/sessions", {
          method: "POST",
          body: JSON.stringify({ task, team }),
        });
        window.location.hash = `#/sessions/${encodeURIComponent(created.id)}`;
      } catch (requestError) {
        error.hidden = false;
        error.textContent = friendlyError(requestError);
        button.disabled = false;
        button.textContent = "회의 시작";
      }
    });
  } catch (error) {
    if (version === routeVersion) errorPage(error, () => renderHome(version));
  }
}

async function renderSessions(version) {
  loading("회의 목록을 정리하는 중입니다");
  try {
    const data = await api("/sessions?limit=200");
    if (version !== routeVersion) return;
    const sessions = data.sessions || [];
    view.innerHTML = `
      <section class="page">
        <header class="page-header"><div><p class="eyebrow">회의록</p><h1>세션</h1><p class="page-intro">진행 중인 논의와 지난 합의의 기록입니다.</p></div><a class="button button-primary" href="#/">새 회의</a></header>
        <div class="panel">
          ${sessions.length ? `<div class="table-wrap"><table class="session-table"><thead><tr><th>상태</th><th>태스크</th><th>팀</th><th>시작</th><th>작업 토큰</th></tr></thead><tbody>${sessions.map((session) => `
            <tr><td>${statusBadge(session)}</td><td><a href="#/sessions/${encodeURIComponent(session.id)}">${escapeHtml(session.task)}</a></td><td>${escapeHtml(session.team_name)}</td><td>${escapeHtml(dateTime(session.created_at))}</td><td>${number(tokenTotals(session.usage).work)}</td></tr>`).join("")}</tbody></table></div>` : empty("저장된 세션이 없습니다.", '<a class="button button-primary" href="#/">첫 회의 시작</a>')}
        </div>
      </section>`;
  } catch (error) {
    if (version === routeVersion) errorPage(error, () => renderSessions(version));
  }
}

function messageMarkup(message, agents) {
  const recipients = (message.recipients || []).includes("*") ? "모두에게" : (message.recipients || []).join(", ");
  const types = { chat: "논의", result_proposal: "결과 제안", vote: "표결" };
  const agentIndex = Math.max(0, agents.findIndex((agent) => agent.name === message.sender));
  return `
    <article class="timeline-item" data-type="${escapeHtml(message.type)}" data-agent-index="${agentIndex % 4}">
      <div class="timeline-head"><strong>${escapeHtml(message.sender)}</strong><time>${escapeHtml(dateTime(message.created_at))}</time></div>
      <div class="session-meta"><span class="message-type">${escapeHtml(types[message.type] || message.type)}</span><span>→ ${escapeHtml(recipients)}</span></div>
      <p>${escapeHtml(message.content)}</p>
      ${message.vote ? `<span class="tag">${escapeHtml(message.vote)}</span>` : ""}
    </article>`;
}

function proposalMarkup(proposal, votes) {
  const related = votes.filter((vote) => vote.proposal_id === proposal.id);
  return `
    <article class="proposal-card">
      <div class="timeline-head"><strong>제안 ${number(proposal.version)}차 · ${escapeHtml(proposal.proposer)}</strong><span class="tag">${escapeHtml(proposal.status || "pending")}</span></div>
      <p>${escapeHtml(proposal.content)}</p>
      ${related.length ? `<div class="tag-list">${related.map((vote) => `<span class="tag">${escapeHtml(vote.voter)} · ${escapeHtml(vote.decision)}</span>`).join("")}</div>` : '<p class="session-meta">아직 기록된 표가 없습니다.</p>'}
    </article>`;
}

function detailMarkup(context) {
  const { session, team, messages, proposals, votes } = context.data;
  const agents = team?.agents || [];
  const termination = team?.termination || {};
  const budget = context.tokenBudget ?? termination.token_budget ?? null;
  const processedLimit = context.processedTokenLimit ?? termination.processed_token_limit ?? null;
  const active = session.status === "running" || session.status === "voting";
  const agentCards = agents.map((agent) => {
    const state = active
      ? (context.agentStates[agent.name] || { state: "idle", detail: "" })
      : { state: "finished", detail: "" };
    const usage = context.perAgentUsage[agent.name] || {};
    const totals = tokenTotals(usage);
    return `<article class="agent-card"><div class="timeline-head"><strong>${escapeHtml(agent.name)}</strong><span class="tag">${escapeHtml(state.state)}</span></div><p>${escapeHtml(agent.role)}</p><div class="session-meta"><span>${escapeHtml(agent.model || team.default_model)}</span><span>작업 ${number(totals.work)}</span><span>캐시 ${number(totals.cacheRead)}</span></div>${state.detail ? `<p class="notice notice-error">${escapeHtml(state.detail)}</p>` : ""}</article>`;
  }).join("");
  const vote = context.voteStatus;
  const votePanel = vote ? `<div class="notice notice-active"><strong>제안 ${number(vote.proposal_version)}차 표결</strong><div class="tag-list"><span class="tag">승인 ${number(vote.approvals?.length)}</span><span class="tag">반대 ${number(vote.rejections?.length)}</span><span class="tag">대기 ${number(vote.pending?.length)}</span><span class="tag">기권 ${number(vote.abstained?.length)}</span></div></div>` : "";
  let outcome = "";
  if (session.status === "completed" && session.result) {
    outcome = `<section class="result-card" aria-labelledby="result-title"><p class="eyebrow">합의된 결론</p><h2 id="result-title">최종 결과</h2><p class="result-content">${escapeHtml(session.result)}</p><div class="session-meta"><span>제출 ${escapeHtml(session.submitted_by)}</span></div><button class="button button-primary" id="copy-result" type="button">결과 복사</button></section>`;
  } else if (session.draft_result) {
    outcome = `<section class="draft-card"><p class="eyebrow">승인되지 않은 초안</p><h2>마지막 제안</h2><p>${escapeHtml(session.draft_result)}</p><div class="session-meta"><span>제안 ${escapeHtml(session.draft_proposer)}</span></div></section>`;
  }
  return `
    <section class="page">
      <header class="page-header"><div><p class="eyebrow">세션 ${escapeHtml(session.id.slice(0, 8))}</p><h1>${escapeHtml(session.task)}</h1><div class="session-meta">${statusBadge(session)}<span>팀 ${escapeHtml(session.team_name)}</span><span>${escapeHtml(dateTime(session.created_at))}</span><span data-elapsed>경과 ${escapeHtml(elapsedTime(session.created_at, session.finished_at))}</span></div></div>${active ? '<button class="button button-danger" id="cancel-session" type="button">세션 취소</button>' : '<a class="button button-quiet" href="#/">새 회의</a>'}</header>
      ${session.status === "failed" ? `<div class="notice notice-error"><strong>${escapeHtml(statusLabel(session.status, session.fail_reason))}</strong><p>${escapeHtml(failureExplanation(session))}</p></div>` : ""}
      ${votePanel}${outcome}
      <div class="detail-grid">
        <div>
          <section class="panel"><div class="section-heading"><div><p class="eyebrow">대등</p><h2>참여 에이전트</h2></div></div><div class="agent-grid">${agentCards || empty("팀 스냅샷을 찾지 못했습니다.")}</div></section>
          <section class="panel"><div class="section-heading"><div><p class="eyebrow">논의</p><h2>메시지 타임라인</h2></div><span>${number(messages.length)}건</span></div><div class="timeline">${messages.length ? [...messages].sort((a, b) => a.sequence - b.sequence).map((message) => messageMarkup(message, agents)).join("") : empty("아직 메시지가 없습니다. 에이전트가 생각을 시작하면 이곳에 표시됩니다.")}</div></section>
        </div>
        <aside>
          <section class="panel"><p class="eyebrow">사용량</p><h2>토큰 예산</h2>${usageMarkup(session.usage, budget, processedLimit, active ? context.budgetPhase : null, active ? termination : {})}</section>
          <section class="panel"><div class="section-heading"><div><p class="eyebrow">의결</p><h2>제안과 표결</h2></div></div><div class="proposal-list">${proposals.length ? [...proposals].sort((a, b) => a.version - b.version).map((proposal) => proposalMarkup(proposal, votes)).join("") : empty("아직 결과 제안이 없습니다.")}</div></section>
        </aside>
      </div>
    </section>`;
}

function wireDetailActions(context) {
  document.querySelector("#copy-result")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(context.data.session.result);
      toast("최종 결과를 복사했습니다.");
    } catch {
      toast("브라우저가 복사를 허용하지 않았습니다. 결과를 직접 선택해 주세요.", "error");
    }
  });
  document.querySelector("#cancel-session")?.addEventListener("click", async (event) => {
    if (!window.confirm("이 세션을 취소할까요? 진행 중인 모델 호출도 중단됩니다.")) return;
    event.currentTarget.disabled = true;
    try {
      const session = await api(`/sessions/${encodeURIComponent(context.id)}/cancel`, { method: "POST" });
      if (context.version !== routeVersion || routePath() !== `/sessions/${context.id}`) return;
      context.data.session = session;
      renderDetail(context);
      closeStream();
      toast("세션을 취소했습니다.");
    } catch (error) {
      if (context.version !== routeVersion || routePath() !== `/sessions/${context.id}`) return;
      event.currentTarget.disabled = false;
      toast(friendlyError(error), "error");
    }
  });
}

function upsert(list, item, key = "id") {
  const index = list.findIndex((current) => current[key] === item[key]);
  if (index === -1) list.push(item);
  else list[index] = { ...list[index], ...item };
}

function mergeRecords(current, fresh, keyOf) {
  const merged = new Map();
  for (const item of [...(current || []), ...(fresh || [])]) {
    const key = keyOf(item);
    merged.set(key, { ...(merged.get(key) || {}), ...item });
  }
  return [...merged.values()];
}

function renderDetail(context) {
  const focusedId = document.activeElement?.id || null;
  view.innerHTML = detailMarkup(context);
  wireDetailActions(context);
  if (focusedId) document.getElementById(focusedId)?.focus({ preventScroll: true });
}

function applyEvent(context, event) {
  if (!event?.event_id || context.seenEvents.has(event.event_id)) return;
  context.seenEvents.add(event.event_id);
  const payload = event.payload || {};
  if (event.type === "session_status") {
    const currentStatus = context.data.session.status;
    const canAdvance = !TERMINAL.has(currentStatus)
      && !(currentStatus === "voting" && payload.status === "running");
    if (canAdvance) {
      context.data.session = {
        ...context.data.session,
        status: payload.status,
        fail_reason: payload.fail_reason,
        fail_detail: payload.fail_detail,
      };
    }
    if (canAdvance && context.voteStatus?.proposal_id) {
      const proposal = context.data.proposals.find(
        (item) => item.id === context.voteStatus.proposal_id,
      );
      if (proposal && payload.status === "running") proposal.status = "rejected";
      if (proposal && payload.status === "completed") proposal.status = "approved";
    }
  } else if (event.type === "message") {
    upsert(context.data.messages, payload);
    if (payload.type === "result_proposal" && !context.data.proposals.some((proposal) => proposal.id === payload.proposal_id)) {
      context.data.proposals.push({
        id: payload.proposal_id,
        proposer: payload.sender,
        content: payload.content,
        version: Math.max(0, ...context.data.proposals.map((proposal) => proposal.version || 0)) + 1,
        status: "pending",
      });
    }
    if (payload.type === "vote") {
      const liveVote = {
        id: payload.id,
        proposal_id: payload.proposal_id,
        voter: payload.sender,
        decision: payload.vote,
        reason: payload.content,
        created_at: payload.created_at,
      };
      const voteIndex = context.data.votes.findIndex(
        (vote) => vote.proposal_id === liveVote.proposal_id && vote.voter === liveVote.voter,
      );
      if (voteIndex === -1) context.data.votes.push(liveVote);
      else context.data.votes[voteIndex] = { ...context.data.votes[voteIndex], ...liveVote };
    }
  } else if (event.type === "agent_state") {
    context.agentStates[payload.agent] = { state: payload.state, detail: payload.detail || "" };
  } else if (event.type === "usage") {
    context.data.session.usage = payload.usage || context.data.session.usage;
    context.tokenBudget = payload.token_budget ?? context.tokenBudget;
    context.processedTokenLimit = payload.processed_token_limit ?? context.processedTokenLimit;
    context.budgetPhase = payload.phase ?? context.budgetPhase;
    context.perAgentUsage = payload.per_agent || context.perAgentUsage;
  } else if (event.type === "vote_status") {
    context.voteStatus = payload;
  } else if (event.type === "result") {
    context.data.session.result = payload.result;
    context.data.session.submitted_by = payload.submitted_by;
  }
  renderDetail(context);
  if (event.type === "message") announce(`${payload.sender || "에이전트"}의 새 메시지가 도착했습니다.`);
  if (event.type === "vote_status") announce("표결 현황이 갱신되었습니다.");
  if (event.type === "result") announce("최종 결과가 확정되었습니다.");
  const terminalReady = context.data.session.status !== "completed" || Boolean(context.data.session.result);
  if (TERMINAL.has(context.data.session.status) && terminalReady) {
    closeStream();
    refreshTerminalDetail(context);
  }
}

async function refreshTerminalDetail(context) {
  try {
    const fresh = await api(`/sessions/${encodeURIComponent(context.id)}`);
    if (context.version !== routeVersion || routePath() !== `/sessions/${context.id}`) return;
    if (!fresh.team) fresh.team = context.data.team;
    fresh.messages = mergeRecords(context.data.messages, fresh.messages, (item) => item.id);
    fresh.proposals = mergeRecords(context.data.proposals, fresh.proposals, (item) => item.id);
    fresh.votes = mergeRecords(
      context.data.votes,
      fresh.votes,
      (item) => `${item.proposal_id}:${item.voter}`,
    );
    context.data = fresh;
    renderDetail(context);
  } catch {
    // SSE로 받은 최종 상태는 이미 표시됐다. 보조 REST refresh 실패는 숨긴다.
  }
}

function connectEvents(context, version) {
  closeStream();
  const source = new EventSource(`/sessions/${encodeURIComponent(context.id)}/events`);
  activeStream = source;
  activeElapsedTimer = window.setInterval(() => {
    if (context.version !== routeVersion) return;
    const elapsed = document.querySelector("[data-elapsed]");
    if (elapsed) elapsed.textContent = `경과 ${elapsedTime(context.data.session.created_at, context.data.session.finished_at)}`;
  }, 1000);
  source.addEventListener("open", () => {
    if (activeStream === source && version === routeVersion) setConnection("connected", "실시간 연결됨");
  });
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (message) => {
      if (activeStream !== source || version !== routeVersion) return;
      try {
        applyEvent(context, JSON.parse(message.data));
      } catch {
        toast("실시간 이벤트 하나를 읽지 못했습니다.", "error");
      }
    });
  }
  source.addEventListener("error", () => {
    if (activeStream !== source || version !== routeVersion) return;
    if (TERMINAL.has(context.data.session.status)) {
      closeStream();
      setConnection("connected", "기록 불러옴");
    } else {
      setConnection("disconnected", "재연결 중");
    }
  });
}

async function renderSessionDetail(id, version) {
  loading("회의 내용을 복원하는 중입니다");
  try {
    const data = await api(`/sessions/${encodeURIComponent(id)}`);
    if (version !== routeVersion) return;
    if (!data.team) {
      const teams = await api("/teams");
      data.team = (teams.teams || []).find((team) => team.name === data.session.team_name) || null;
    }
    const context = {
      id,
      version,
      data,
      seenEvents: new Set(),
      agentStates: {},
      perAgentUsage: {},
      tokenBudget: data.team?.termination?.token_budget || null,
      processedTokenLimit: data.team?.termination?.processed_token_limit || null,
      budgetPhase: null,
      voteStatus: null,
    };
    renderDetail(context);
    connectEvents(context, version);
  } catch (error) {
    if (version === routeVersion) errorPage(error, () => renderSessionDetail(id, version));
  }
}

async function renderTeams(version) {
  loading("참여 팀을 불러오는 중입니다");
  try {
    const data = await api("/teams");
    if (version !== routeVersion) return;
    const teams = data.teams || [];
    view.innerHTML = `
      <section class="page">
        <header class="page-header"><div><p class="eyebrow">구성</p><h1>팀</h1><p class="page-intro">팀 구성은 YAML에 보존되며 이 화면에서는 읽기만 할 수 있습니다.</p></div></header>
        <div class="team-grid">${teams.map((team) => `
          <article class="team-card">
            <div class="timeline-head"><div><p class="eyebrow">${escapeHtml(profileLabel(team))}</p><h2>${escapeHtml(team.description || team.name)}</h2></div><span class="tag">${escapeHtml(team.default_model)}</span></div>
            <div class="session-meta"><span>메시지 ${number(team.termination.max_messages)}</span><span>작업 예산 ${number(team.termination.token_budget)}</span>${team.termination.processed_token_limit ? `<span>처리 상한 ${number(team.termination.processed_token_limit)}</span>` : ""}${team.termination.synthesis_at ? `<span>종합 시작 ${number(team.termination.synthesis_at)}</span>` : ""}${team.termination.proposal_by ? `<span>제안 강제 ${number(team.termination.proposal_by)}</span>` : ""}${team.termination.call_reserve_tokens ? `<span>호출 예약 ${number(team.termination.call_reserve_tokens)}</span>` : ""}${team.termination.max_proposals ? `<span>제안 최대 ${number(team.termination.max_proposals)}회</span>` : ""}<span>유휴 ${number(team.termination.idle_timeout)}초</span><span>승인 ${escapeHtml(team.termination.approval.mode)}</span><span>투표 제한 ${number(team.termination.approval.voting_timeout)}초</span>${team.termination.approval.minimum_votes ? `<span>최소 ${number(team.termination.approval.minimum_votes)}표</span>` : ""}</div>
            <div class="agent-grid">${(team.agents || []).map((agent) => `<section class="agent-card"><strong>${escapeHtml(agent.name)}</strong><p>${escapeHtml(agent.role)}</p><div class="session-meta"><span>${escapeHtml(agent.model)}</span><span>최대 ${number(agent.max_turns)}턴</span></div><div class="tag-list">${(agent.capabilities || []).map((capability) => `<span class="tag">${escapeHtml(capability)}</span>`).join("")}</div></section>`).join("")}</div>
          </article>`).join("") || empty("설정된 팀이 없습니다.")}</div>
      </section>`;
  } catch (error) {
    if (version === routeVersion) errorPage(error, () => renderTeams(version));
  }
}

async function route() {
  closeStream();
  const version = ++routeVersion;
  const path = routePath();
  updateNavigation(path);
  if (path === "/") await renderHome(version);
  else if (path === "/sessions") await renderSessions(version);
  else if (path === "/teams") await renderTeams(version);
  else if (/^\/sessions\/[^/]+$/.test(path)) {
    await renderSessionDetail(decodeURIComponent(path.split("/")[2]), version);
  } else {
    view.innerHTML = `<section class="page"><div class="empty-state"><p class="eyebrow">404</p><h1>이 화면은 찾을 수 없습니다</h1><a class="button button-primary" href="#/">홈으로</a></div></section>`;
  }
  if (version === routeVersion) window.requestAnimationFrame(() => view.focus({ preventScroll: true }));
}

async function checkHealth() {
  try {
    const response = await fetch("/health", { cache: "no-store" });
    if (!response.ok) throw new Error("unhealthy");
    if (!activeStream) setConnection("connected", "서버 연결됨");
  } catch {
    setConnection("disconnected", "서버 연결 끊김");
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", closeStream);
window.setInterval(checkHealth, 15000);
route();
