// Campus Lost & Found - small helper scripts (no frameworks needed)
document.addEventListener("DOMContentLoaded", () => {
  // Mobile menu
  const toggle = document.querySelector(".nav-toggle");
  const links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", () => {
      const open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // Confirm dialogs: <form data-confirm="Are you sure?">
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // Image preview for file inputs
  document.querySelectorAll("input[type=file][data-preview]").forEach((input) => {
    const target = document.querySelector(input.dataset.preview);
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file || !target) return;
      if (file.size > 6 * 1024 * 1024) {
        alert("Please choose a photo smaller than 6 MB.");
        input.value = "";
        return;
      }
      target.src = URL.createObjectURL(file);
      target.classList.remove("hide");
      if (input.dataset.describe) describePhoto(input, file);
    });
  });

  // ---------- AI: write the description from the photo ----------
  // Fills empty fields, or fields the AI filled earlier. Never overwrites what the student typed.
  async function describePhoto(input, file) {
    const status = document.getElementById("ai-status");
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const fields = {
      name: document.getElementById("name"),
      description: document.getElementById("description"),
      color: document.getElementById("color"),
      brand: document.getElementById("brand"),
      distinctive_features: document.getElementById("distinctive_features"),
    };
    const say = (t, bad) => { if (status) { status.textContent = t; status.classList.toggle("ai-bad", !!bad); } };
    say("✨ AI is looking at your photo…");
    input.disabled = true;
    try {
      const body = new FormData();
      body.append("image", file);
      const res = await fetch(input.dataset.describe, { method: "POST", body, headers: { "X-CSRF-Token": csrf } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { say(data.error || "The AI could not read this photo. Please type the description.", true); return; }
      let filled = 0;
      Object.entries(fields).forEach(([key, el]) => {
        const val = (data[key] || "").trim();
        if (!el || !val) return;
        if (el.value.trim() === "" || el.dataset.ai === "1") {
          el.value = val; el.dataset.ai = "1"; filled++;
          el.addEventListener("input", () => { el.dataset.ai = "0"; }, { once: true });
        }
      });
      say(filled
        ? (data.source === "claude" ? "✨ Description written by AI from your photo. Please check and edit it."
                                    : "✨ Basic description added from the photo's colours and shape. Please add what the item is.")
        : "Your own text was kept. The photo was not used to change it.");
    } catch (err) {
      say("Network problem. Please type the description.", true);
    } finally {
      input.disabled = false;
    }
  }

  // ---------- Pop-up when a new chat message arrives (not shown inside Messages) ----------
  const body = document.body;
  if (body.dataset.role === "student") {
    const uid = body.dataset.uid;
    const key = "lf_seen_msg_" + uid;
    const serverMax = parseInt(body.dataset.msgMax || "0", 10);
    const getSeen = () => { try { const v = localStorage.getItem(key); return v === null ? null : parseInt(v, 10); } catch (e) { return null; } };
    const setSeen = (v) => { try { localStorage.setItem(key, String(v)); } catch (e) { /* storage blocked */ } };
    let seen = getSeen();
    if (seen === null) { seen = serverMax; setSeen(seen); }
    const inMessages = body.dataset.page === "chat";
    const wrap = document.getElementById("toasts");

    const toast = (m) => {
      if (!wrap) return;
      const a = document.createElement("a");
      a.className = "toast";
      a.href = "/chat/" + m.enquiry_id;
      const who = document.createElement("b");
      who.textContent = m.sender + " sent a message";
      const about = document.createElement("span");
      about.className = "toast-about";
      about.textContent = "About: " + m.item;
      const txt = document.createElement("span");
      txt.textContent = m.text;
      const x = document.createElement("button");
      x.type = "button"; x.className = "toast-x"; x.setAttribute("aria-label", "Dismiss"); x.textContent = "×";
      x.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); a.remove(); });
      a.append(who, about, txt, x);
      wrap.appendChild(a);
      while (wrap.children.length > 3) wrap.firstChild.remove();
      setTimeout(() => a.remove(), 9000);
    };

    const check = async () => {
      try {
        const res = await fetch("/api/new-messages?after=" + seen, { headers: { "Accept": "application/json" } });
        if (!res.ok) return;
        const data = await res.json();
        if (!inMessages) data.messages.forEach(toast);
        // inside Messages the chat itself shows them, so just move the marker on
        seen = Math.max(seen, data.latest);
        setSeen(seen);
      } catch (err) { /* ignore temporary network errors */ }
    };
    if (inMessages) setSeen(Math.max(seen, serverMax));
    check();
    setInterval(check, 5000);
  }

  // Fade out success messages
  document.querySelectorAll(".flash.success").forEach((el) => {
    setTimeout(() => { el.style.transition = "opacity .5s"; el.style.opacity = "0"; }, 5000);
    setTimeout(() => el.remove(), 5600);
  });

  // ---------- Live chat ----------
  const box = document.getElementById("messages");
  if (box) {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const id = box.dataset.enquiry;
    let last = parseInt(box.dataset.last || "0", 10);
    const form = document.getElementById("composer");
    const input = form.querySelector("input[name=message]");

    const add = (m) => {
      if (box.querySelector(`[data-id="${m.id}"]`)) return;
      const div = document.createElement("div");
      div.className = "bubble " + (m.mine ? "mine" : "theirs");
      div.dataset.id = m.id;
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = m.mine ? "You" : (box.dataset.other || "Other");
      div.appendChild(who);
      div.appendChild(document.createTextNode(m.text));   // text node => safe from HTML injection
      const t = document.createElement("time");
      t.textContent = m.time;
      div.appendChild(t);
      box.appendChild(div);
      last = Math.max(last, m.id);
      box.scrollTop = box.scrollHeight;
    };
    box.scrollTop = box.scrollHeight;

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const body = new FormData();
      body.append("message", text);
      body.append("_csrf", csrf);
      try {
        const res = await fetch(`/chat/${id}/send`, { method: "POST", body, headers: { "X-Requested-With": "fetch" } });
        if (res.ok) { add(await res.json()); input.value = ""; box.querySelector(".empty-chat")?.remove(); }
        else { const j = await res.json().catch(() => ({})); alert(j.error || "Message could not be sent."); }
      } catch (err) { alert("Network problem. Please try again."); }
    });

    setInterval(async () => {
      try {
        const res = await fetch(`/chat/${id}/poll?after=${last}`);
        if (res.ok) { (await res.json()).forEach(add); box.querySelector(".empty-chat")?.remove(); }
      } catch (err) { /* ignore temporary network errors */ }
    }, 4000);
  }
});
