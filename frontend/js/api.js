const BASE = "http://localhost:8000/api";

async function _fetch(path, options = {}) {
    const res = await fetch(BASE + path, options);
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || "Request failed");
    }
    return res;
}

export async function getJson(path) {
    return (await _fetch(path)).json();
}

export async function postJson(path, body) {
    return (await _fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    })).json();
}

export async function postForm(path, formData) {
    return (await _fetch(path, {
        method: "POST",
        body: formData,
    })).json();
}

export function downloadUrl(path) {
    return BASE + path;
}
