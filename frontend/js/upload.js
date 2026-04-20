import { getJson, postForm } from "./api.js";

const dropZone   = document.getElementById("drop-zone");
const fileInput  = document.getElementById("file-input");
const fileInfo   = document.getElementById("file-info");
const btnUpload  = document.getElementById("btn-upload");
const statusBox  = document.getElementById("status-box");
const doneLink   = document.getElementById("done-link");
const meetingLink = document.getElementById("view-meeting-link");
const projectSel = document.getElementById("project-select");

let selectedFile = null;
let pollTimer    = null;

// ── Load projects into select ─────────────────────────────────────────────────
async function loadProjects() {
    try {
        const projects = await getJson("/projects");
        projects.forEach(p => {
            const opt = document.createElement("option");
            opt.value = p.id;
            opt.textContent = p.name;
            projectSel.appendChild(opt);
        });
    } catch (_) {}
}

// ── Drag-drop ─────────────────────────────────────────────────────────────────
dropZone.addEventListener("click", () => fileInput.click());

dropZone.addEventListener("dragover", e => {
    e.preventDefault();
    dropZone.classList.add("dragover");
});

dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));

dropZone.addEventListener("drop", e => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    const file = e.dataTransfer.files[0];
    if (file) setFile(file);
});

fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) setFile(fileInput.files[0]);
});

function setFile(file) {
    const ext = file.name.split(".").pop().toLowerCase();
    if (!["txt", "vtt"].includes(ext)) {
        showStatus("Only .txt and .vtt files are allowed.", "error");
        return;
    }
    selectedFile = file;
    dropZone.classList.add("has-file");
    fileInfo.style.display = "block";
    fileInfo.textContent = `${file.name}  (${(file.size / 1024).toFixed(1)} KB)`;
    btnUpload.disabled = false;
    clearStatus();
}

// ── Upload ────────────────────────────────────────────────────────────────────
btnUpload.addEventListener("click", async () => {
    if (!selectedFile) return;

    btnUpload.disabled = true;
    doneLink.style.display = "none";
    showStatus("Uploading…", "info");

    const fd = new FormData();
    fd.append("file", selectedFile);
    const projectId = projectSel.value;
    if (projectId) fd.append("project_id", projectId);

    try {
        const meeting = await postForm("/meetings/upload", fd);
        showStatus(`Uploaded! Processing in background… (meeting #${meeting.id})`, "info");
        startPolling(meeting.id);
    } catch (e) {
        showStatus(`Upload failed: ${e.message}`, "error");
        btnUpload.disabled = false;
    }
});

// ── Polling ───────────────────────────────────────────────────────────────────
function startPolling(meetingId) {
    let attempts = 0;

    pollTimer = setInterval(async () => {
        attempts++;
        try {
            const status = await getJson(`/meetings/${meetingId}/status`);

            if (status.processed) {
                clearInterval(pollTimer);
                showStatus("Processing complete! Your meeting is ready.", "success");
                meetingLink.href = `meeting.html?id=${meetingId}`;
                doneLink.style.display = "block";
                return;
            }

            if (status.error) {
                clearInterval(pollTimer);
                showStatus(`Processing failed: ${status.error}`, "error");
                btnUpload.disabled = false;
                return;
            }

            // Still running — update message with task state
            showStatus(
                `Processing… (${status.task_status.toLowerCase()}) — this may take 20–60 seconds.`,
                "info"
            );

            if (attempts > 90) {          // 3 minutes max
                clearInterval(pollTimer);
                showStatus("Processing is taking longer than expected. Check back later.", "error");
                btnUpload.disabled = false;
            }
        } catch (e) {
            console.error("poll error:", e);
        }
    }, 2000);
}

// ── Status helper ─────────────────────────────────────────────────────────────
function showStatus(msg, type) {
    statusBox.textContent = msg;
    statusBox.className = `status-box ${type}`;
    statusBox.style.display = "block";
}

function clearStatus() {
    statusBox.style.display = "none";
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadProjects();
