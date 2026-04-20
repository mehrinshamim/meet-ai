import { getJson, postJson } from "./api.js";

// ── Projects map keyed by id (populated when projects load) ─────────────────
const projectsById = {};

// ── Stats ────────────────────────────────────────────────────────────────────
async function loadStats() {
    try {
        const s = await getJson("/stats");
        document.getElementById("s-meetings").textContent   = s.total_meetings;
        document.getElementById("s-processed").textContent  = s.processed_meetings;
        document.getElementById("s-projects").textContent   = s.total_projects;
        document.getElementById("s-decisions").textContent  = s.total_decisions;
        document.getElementById("s-actions").textContent    = s.total_action_items;
    } catch (e) {
        console.error("stats error:", e);
    }
}

// ── Projects ─────────────────────────────────────────────────────────────────
async function loadProjects() {
    const grid = document.getElementById("projects-grid");
    try {
        const projects = await getJson("/projects");
        document.getElementById("projects-count").textContent = `(${projects.length})`;
        projects.forEach(p => { projectsById[p.id] = p; });

        if (projects.length === 0) {
            grid.innerHTML = '<p class="empty">No projects yet. Create one to organise meetings.</p>';
            return;
        }

        grid.innerHTML = projects.map(p => `
            <div class="card">
                <div class="card-title">${esc(p.name)}</div>
                <div class="card-desc">${esc(p.description || "No description")}</div>
                <div class="card-meta">
                    <span><strong>${p.meeting_count}</strong> meetings</span>
                    <span><strong>${p.action_item_count}</strong> action items</span>
                </div>
            </div>
        `).join("");
    } catch (e) {
        grid.innerHTML = `<p class="empty">Could not load projects: ${esc(e.message)}</p>`;
    }
}

// ── Meetings ─────────────────────────────────────────────────────────────────
async function loadMeetings() {
    const tbody = document.getElementById("meetings-tbody");
    try {
        const meetings = await getJson("/meetings");

        if (meetings.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" class="empty">No meetings yet. <a href="upload.html">Upload one</a>.</td></tr>';
            return;
        }

        tbody.innerHTML = meetings.map(m => {
            const projectName = m.project_id && projectsById[m.project_id]
                ? esc(projectsById[m.project_id].name)
                : '<span class="text-muted">—</span>';
            const speakers = m.speaker_names && m.speaker_names.length
                ? esc(m.speaker_names.slice(0, 3).join(", ")) + (m.speaker_names.length > 3 ? "…" : "")
                : '<span class="text-muted">—</span>';
            const badge = statusBadge(m);
            const date = new Date(m.created_at).toLocaleDateString();

            return `<tr>
                <td><span title="${esc(m.filename)}">${esc(truncate(m.filename, 32))}</span></td>
                <td><span class="badge badge-muted">${esc(m.file_format.toUpperCase())}</span></td>
                <td>${projectName}</td>
                <td>${speakers}</td>
                <td>${badge}</td>
                <td class="text-muted">${date}</td>
                <td>${m.processed ? `<a class="btn btn-sm btn-outline" href="meeting.html?id=${m.id}">View</a>` : ""}</td>
            </tr>`;
        }).join("");
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty">Could not load meetings: ${esc(e.message)}</td></tr>`;
    }
}

function statusBadge(m) {
    if (m.error)      return '<span class="badge badge-danger">Error</span>';
    if (m.processed)  return '<span class="badge badge-success">Ready</span>';
    return '<span class="badge badge-warning">Processing</span>';
}

// ── New project modal ─────────────────────────────────────────────────────────
function setupModal() {
    const modal   = document.getElementById("modal-project");
    const errEl   = document.getElementById("modal-err");
    const nameIn  = document.getElementById("proj-name");
    const descIn  = document.getElementById("proj-desc");

    document.getElementById("btn-new-project").addEventListener("click", () => {
        modal.classList.add("open");
        nameIn.value = "";
        descIn.value = "";
        errEl.style.display = "none";
        nameIn.focus();
    });

    document.getElementById("btn-cancel-modal").addEventListener("click", () => {
        modal.classList.remove("open");
    });

    modal.addEventListener("click", e => {
        if (e.target === modal) modal.classList.remove("open");
    });

    document.getElementById("btn-save-project").addEventListener("click", async () => {
        const name = nameIn.value.trim();
        if (!name) { showErr("Project name is required."); return; }

        try {
            await postJson("/projects", { name, description: descIn.value.trim() || null });
            modal.classList.remove("open");
            await Promise.all([loadStats(), loadProjects(), loadMeetings()]);
        } catch (e) {
            showErr(e.message);
        }
    });

    function showErr(msg) {
        errEl.textContent = msg;
        errEl.style.display = "block";
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function truncate(s, n) {
    return s.length > n ? s.slice(0, n) + "…" : s;
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
    setupModal();
    await loadProjects();          // load projects first so names are ready
    await Promise.all([loadStats(), loadMeetings()]);
}

init();
