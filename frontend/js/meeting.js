import { getJson, postJson, downloadUrl } from "./api.js";

// ── Parse meeting id from URL ─────────────────────────────────────────────────
const params    = new URLSearchParams(location.search);
const meetingId = parseInt(params.get("id") || "0", 10);

if (!meetingId) {
    document.getElementById("meeting-title").textContent = "Invalid meeting URL";
}

// ── State ─────────────────────────────────────────────────────────────────────
let sessionId    = null;
let scopeAll     = false;       // false = this meeting, true = all meetings
let pollTimer    = null;
let chatLoading  = false;

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
    if (!meetingId) return;

    // Set CSV export link immediately
    document.getElementById("export-csv-btn").href =
        downloadUrl(`/meetings/${meetingId}/extractions/export`);

    await checkStatus();
}

// ── Status polling ─────────────────────────────────────────────────────────────
async function checkStatus() {
    try {
        const status = await getJson(`/meetings/${meetingId}/status`);

        if (status.processed) {
            clearInterval(pollTimer);
            showMain();
        } else if (status.error) {
            clearInterval(pollTimer);
            document.getElementById("meeting-title").textContent = "Processing failed";
            document.getElementById("meeting-meta").textContent = status.error;
        } else {
            document.getElementById("processing-banner").style.display = "block";
            document.getElementById("meeting-title").textContent = `Meeting #${meetingId}`;
            if (!pollTimer) {
                pollTimer = setInterval(checkStatus, 3000);
            }
        }
    } catch (e) {
        document.getElementById("meeting-title").textContent = `Error: ${e.message}`;
    }
}

// ── Show main content ─────────────────────────────────────────────────────────
async function showMain() {
    document.getElementById("processing-banner").style.display = "none";
    document.getElementById("main-content").style.display = "block";

    // Load meeting metadata via status endpoint (we have basic info)
    // Try to get full info by loading extractions first (which validates meeting exists)
    loadMeetingMeta();

    // Load all panels in parallel
    loadExtractions();
    loadSentiment();
    initChat();
}

async function loadMeetingMeta() {
    try {
        // There's no dedicated GET /meetings/{id} — derive what we can
        // from the meetings list (small payload, cached by browser)
        const meetings = await getJson("/meetings");
        const m = meetings.find(x => x.id === meetingId);
        if (m) {
            document.getElementById("meeting-title").textContent = m.filename;
            const meta = [];
            if (m.file_format) meta.push(m.file_format.toUpperCase());
            if (m.word_count)  meta.push(`${m.word_count.toLocaleString()} words`);
            if (m.speaker_names && m.speaker_names.length) {
                meta.push(`Speakers: ${m.speaker_names.join(", ")}`);
            }
            document.getElementById("meeting-meta").textContent = meta.join(" · ");
        } else {
            document.getElementById("meeting-title").textContent = `Meeting #${meetingId}`;
        }
    } catch (_) {
        document.getElementById("meeting-title").textContent = `Meeting #${meetingId}`;
    }
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
        tab.classList.add("active");
        document.getElementById(`panel-${tab.dataset.tab}`).classList.add("active");
    });
});

// ── Panel 1: Extractions ──────────────────────────────────────────────────────
async function loadExtractions() {
    try {
        const data = await getJson(`/meetings/${meetingId}/extractions`);
        renderDecisions(data.decisions || []);
        renderActions(data.action_items || []);
    } catch (e) {
        document.getElementById("decisions-list").innerHTML =
            `<li class="empty">Could not load extractions: ${esc(e.message)}</li>`;
        document.getElementById("actions-tbody").innerHTML =
            `<tr><td colspan="4" class="empty">${esc(e.message)}</td></tr>`;
    }
}

function renderDecisions(decisions) {
    const ul = document.getElementById("decisions-list");
    if (!decisions.length) {
        ul.innerHTML = '<li class="empty">No decisions found.</li>';
        return;
    }
    ul.innerHTML = decisions.map(d => `
        <li>
            ${esc(d.text || "")}
            <div class="meta">
                ${d.speaker ? `<strong>${esc(d.speaker)}</strong>` : ""}
                ${d.timestamp ? ` · ${esc(d.timestamp)}` : ""}
            </div>
        </li>
    `).join("");
}

function renderActions(items) {
    const tbody = document.getElementById("actions-tbody");
    if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty">No action items found.</td></tr>';
        return;
    }
    tbody.innerHTML = items.map(a => `
        <tr>
            <td>${esc(a.task || "")}</td>
            <td>${esc(a.assignee || "—")}</td>
            <td>${esc(a.due_date || "—")}</td>
            <td class="text-muted">${esc(a.timestamp || "")}</td>
        </tr>
    `).join("");
}

// ── Panel 2: Sentiment ────────────────────────────────────────────────────────
async function loadSentiment() {
    try {
        const data = await getJson(`/meetings/${meetingId}/sentiment`);
        if (data.speaker_scores && Object.keys(data.speaker_scores).length) {
            drawSentimentChart(
                document.getElementById("sentiment-canvas"),
                data.speaker_scores
            );
        } else {
            document.querySelector(".chart-wrap").innerHTML =
                '<p class="empty">No speaker sentiment data.</p>';
        }
        renderTimeline(data.segment_scores || []);
    } catch (e) {
        document.getElementById("panel-sentiment").innerHTML =
            `<p class="empty">Could not load sentiment: ${esc(e.message)}</p>`;
    }
}

function drawSentimentChart(canvas, speakerScores) {
    const ctx      = canvas.getContext("2d");
    const entries  = Object.entries(speakerScores);
    const ROW_H    = 44;
    const PAD      = { top: 24, left: 130, right: 60, bottom: 8 };

    canvas.width  = canvas.parentElement.offsetWidth || 600;
    canvas.height = entries.length * ROW_H + PAD.top + PAD.bottom;

    const chartW = canvas.width - PAD.left - PAD.right;
    const midX   = PAD.left + chartW / 2;

    // Grid lines
    ctx.strokeStyle = "#e5e7eb";
    ctx.lineWidth = 1;
    [-1, -0.5, 0, 0.5, 1].forEach(v => {
        const x = PAD.left + ((v + 1) / 2) * chartW;
        ctx.beginPath();
        ctx.moveTo(x, PAD.top - 8);
        ctx.lineTo(x, canvas.height - PAD.bottom);
        ctx.stroke();
        ctx.fillStyle = "#9ca3af";
        ctx.font = "10px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(v === 0 ? "0" : v.toFixed(1), x, PAD.top - 10);
    });

    entries.forEach(([speaker, score], i) => {
        const cy = PAD.top + i * ROW_H + ROW_H / 2;

        // Speaker label
        ctx.fillStyle = "#111827";
        ctx.font = "13px sans-serif";
        ctx.textAlign = "right";
        ctx.fillText(speaker.length > 16 ? speaker.slice(0, 14) + "…" : speaker, PAD.left - 10, cy + 5);

        // Bar
        const pct    = (score + 1) / 2;              // 0..1
        const barW   = Math.abs(score / 2) * chartW;
        const barX   = score >= 0 ? midX : midX - barW;
        const color  = score > 0.3 ? "#059669" : score < -0.3 ? "#dc2626" : "#9ca3af";
        ctx.fillStyle = color;
        ctx.fillRect(barX, cy - 13, Math.max(barW, 2), 26);

        // Score label
        ctx.fillStyle = "#111827";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "left";
        const labelX = score >= 0 ? midX + barW + 6 : PAD.left + chartW + 6;
        ctx.fillText(score.toFixed(2), labelX, cy + 5);
    });
}

function renderTimeline(segments) {
    const container = document.getElementById("segment-timeline");
    const detail    = document.getElementById("segment-detail");

    if (!segments.length) {
        container.innerHTML = '<p class="empty">No segment data.</p>';
        return;
    }

    container.innerHTML = segments.map((seg, i) => {
        const cls = seg.label === "positive" ? "positive"
                  : seg.label === "negative" ? "negative"
                  : "neutral";
        return `<div class="segment ${cls}"
            data-index="${i}"
            title="${esc(seg.speaker)} · ${esc(seg.start_time)} · ${esc(seg.label)} (${seg.score.toFixed(2)})">
        </div>`;
    }).join("");

    container.querySelectorAll(".segment").forEach(el => {
        el.addEventListener("click", () => {
            const i   = parseInt(el.dataset.index, 10);
            const seg = segments[i];
            detail.innerHTML = `
                <strong>${esc(seg.speaker)}</strong> at ${esc(seg.start_time)}<br>
                Score: <strong style="color:${scoreColor(seg.score)}">${seg.score.toFixed(2)}</strong>
                — <em>${esc(seg.label)}</em>
                <span class="text-muted" style="font-size:11px;margin-left:8px;">chunk #${seg.chunk_id}</span>
            `;
            detail.classList.add("open");
        });
    });
}

function scoreColor(score) {
    return score > 0.3 ? "#059669" : score < -0.3 ? "#dc2626" : "#6b7280";
}

// ── Panel 3: Chat ─────────────────────────────────────────────────────────────
function initChat() {
    // Persist session per meeting across page reloads
    const storageKey = `meetai_session_${meetingId}`;
    sessionId = localStorage.getItem(storageKey) || null;

    if (sessionId) {
        document.getElementById("chat-session-label").textContent = `session: ${sessionId.slice(0, 8)}…`;
        loadChatHistory();
    }

    document.getElementById("scope-meeting").addEventListener("click", () => {
        scopeAll = false;
        document.getElementById("scope-meeting").classList.add("active");
        document.getElementById("scope-all").classList.remove("active");
    });
    document.getElementById("scope-all").addEventListener("click", () => {
        scopeAll = true;
        document.getElementById("scope-all").classList.add("active");
        document.getElementById("scope-meeting").classList.remove("active");
    });

    const input  = document.getElementById("chat-input");
    const sendBtn = document.getElementById("chat-send");

    sendBtn.addEventListener("click", sendMessage);
    input.addEventListener("keydown", e => {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
}

async function loadChatHistory() {
    try {
        const data = await getJson(`/chat/history?session_id=${encodeURIComponent(sessionId)}`);
        const msgs = document.getElementById("chat-messages");
        msgs.innerHTML = "";
        data.messages.forEach(m => {
            appendMessage("user", m.question);
            appendMessage("bot", m.answer, m.citations);
        });
    } catch (_) {
        // 404 = no history yet, that's fine
    }
}

async function sendMessage() {
    if (chatLoading) return;
    const input = document.getElementById("chat-input");
    const q     = input.value.trim();
    if (!q) return;

    input.value = "";
    chatLoading  = true;
    document.getElementById("chat-send").disabled = true;

    appendMessage("user", q);

    // Typing indicator
    const thinkEl = appendMessage("bot", "…");

    try {
        const body = { question: q, session_id: sessionId || undefined };
        if (!scopeAll) body.meeting_id = meetingId;

        const resp = await postJson("/chat", body);

        // Persist session_id for future visits
        if (!sessionId) {
            sessionId = resp.session_id;
            localStorage.setItem(`meetai_session_${meetingId}`, sessionId);
            document.getElementById("chat-session-label").textContent =
                `session: ${sessionId.slice(0, 8)}…`;
        }

        thinkEl.querySelector(".bubble").textContent = resp.answer;
        renderCitations(thinkEl, resp.citations);
    } catch (e) {
        thinkEl.querySelector(".bubble").textContent = `Error: ${e.message}`;
    } finally {
        chatLoading = false;
        document.getElementById("chat-send").disabled = false;
    }
}

function appendMessage(role, text, citations) {
    const msgs = document.getElementById("chat-messages");

    // Clear placeholder on first real message
    if (msgs.querySelector(".empty")) msgs.innerHTML = "";

    const wrap = document.createElement("div");
    wrap.className = `msg msg-${role === "user" ? "user" : "bot"}`;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    wrap.appendChild(bubble);

    if (citations && citations.length) {
        renderCitations(wrap, citations);
    }

    msgs.appendChild(wrap);
    msgs.scrollTop = msgs.scrollHeight;
    return wrap;
}

function renderCitations(wrap, citations) {
    if (!citations || !citations.length) return;
    const row = wrap.querySelector(".msg-citations") || document.createElement("div");
    row.className = "msg-citations";
    row.innerHTML = citations.map(c =>
        `<span class="citation-chip" title="Speaker: ${esc(c.speaker || "")}">${esc(c.meeting || "")} · ${esc(c.time || "")}</span>`
    ).join("");
    if (!wrap.contains(row)) wrap.appendChild(row);
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

// ── Start ─────────────────────────────────────────────────────────────────────
init();
