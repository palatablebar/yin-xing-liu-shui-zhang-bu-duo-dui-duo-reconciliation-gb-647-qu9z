const state = { timer: null, busy: false };

function flash(message, kind) {
  const box = document.querySelector("#flash");
  if (!box) return;
  box.textContent = message;
  box.className = kind === "ok" ? "flash ok" : "flash";
  box.hidden = false;
  window.clearTimeout(state.timer);
  state.timer = window.setTimeout(() => { box.hidden = true; }, 6000);
}

async function post(url, body) {
  const options = { method: "POST" };
  if (body) options.body = body;
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.message || data.detail || "请求失败");
  }
  return data;
}

function setBusy(value) {
  state.busy = value;
  document.querySelectorAll("[data-action]").forEach((button) => {
    button.disabled = value;
  });
}

async function runAction(name) {
  if (state.busy) return;
  if (name === "quarantine") {
    window.location.href = "/quarantine";
    return;
  }
  const urls = {
    demo: "/api/demo/run",
    candidates: "/api/candidates",
    matches: "/api/matches",
  };
  const url = urls[name];
  if (!url) return;
  setBusy(true);
  try {
    await post(url);
    window.location.reload();
  } catch (error) {
    flash(error.message);
  } finally {
    setBusy(false);
  }
}

async function postSettlement(policy) {
  if (state.busy) return;
  setBusy(true);
  try {
    const url = policy ? "/api/settlement?policy=" + encodeURIComponent(policy)
      : "/api/settlement";
    const data = await post(url);
    flash(data.created ? "已记账 " + data.summary.posted_amount + " 元" : "该方案已记过账，未重复写入", "ok");
    window.location.reload();
  } catch (error) {
    flash(error.message);
  } finally {
    setBusy(false);
  }
}

async function revokePlan(planId) {
  if (state.busy) return;
  if (!window.confirm("撤销这份计划？分录会保留为审计证据，但不再参与余额计算。")) return;
  setBusy(true);
  try {
    await post("/api/settlement/" + encodeURIComponent(planId) + "/revoke");
    flash("计划已撤销，可以按另一策略重记", "ok");
    window.location.reload();
  } catch (error) {
    flash(error.message);
  } finally {
    setBusy(false);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("click", (event) => {
    const policyButton = event.target.closest("[data-policy]");
    if (policyButton) {
      postSettlement(policyButton.dataset.policy);
      return;
    }
    const revokeButton = event.target.closest("[data-revoke]");
    if (revokeButton) {
      revokePlan(revokeButton.dataset.revoke);
      return;
    }
    const button = event.target.closest("[data-action]");
    if (button) runAction(button.dataset.action);
  });

  const form = document.querySelector("#import-form");
  if (form) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const fileInput = form.querySelector('input[type="file"]');
      if (!fileInput || !fileInput.files.length) {
        flash("请选择文件");
        return;
      }
      const data = new FormData(form);
      try {
        await post("/api/statements/import", data);
        window.location.reload();
      } catch (error) {
        flash(error.message);
      }
    });
  }
});
