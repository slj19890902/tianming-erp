(function attachPrintRecovery(global) {
  "use strict";

  function parseErrorDetail(detail) {
    if (typeof detail === "string") return detail.trim();
    if (Array.isArray(detail)) {
      return detail.map((item) => parseErrorDetail(item)).filter(Boolean).join("；");
    }
    if (!detail || typeof detail !== "object") return "";
    const message = parseErrorDetail(
      detail.message ?? detail.detail ?? detail.error ?? detail.msg,
    );
    const code = String(detail.code || "").trim();
    return [message, code ? `错误代码 ${code}` : ""].filter(Boolean).join("；");
  }

  function requestIdOf(response, body) {
    const headerValue = response?.headers?.get?.("x-request-id")
      || response?.headers?.get?.("x-correlation-id")
      || "";
    return String(headerValue || body?.request_id || body?.requestId || "").trim();
  }

  function httpError(parts, status, requestId) {
    const error = new Error(parts.filter(Boolean).join("；"));
    error.status = Number(status || 0);
    error.requestId = String(requestId || "");
    return error;
  }

  async function requestPrintData(url, signal, messages = {}) {
    const response = await fetch(url, {
      credentials: "include",
      signal,
    });
    const raw = await response.text();
    let body = null;
    if (raw.trim()) {
      try { body = JSON.parse(raw); } catch (_error) { body = null; }
    }
    const requestId = requestIdOf(response, body);
    if (response.status === 401) {
      throw httpError([
        messages.auth || "登录已失效，请返回 ERP 登录后重试。",
        "HTTP 401",
        requestId ? `请求编号 ${requestId}` : "",
      ], response.status, requestId);
    }
    if (!response.ok) {
      const detail = parseErrorDetail(body?.detail ?? body);
      const fallback = response.status >= 500
        ? "服务器内部错误，打印内容未能读取。"
        : (messages.failure || "打印内容读取失败。");
      throw httpError([
        detail || fallback,
        `HTTP ${response.status}`,
        requestId ? `请求编号 ${requestId}` : "",
      ], response.status, requestId);
    }
    if (!body || typeof body !== "object") {
      throw httpError([
        "服务器返回了空白或无法识别的打印内容",
        `HTTP ${response.status}`,
        requestId ? `请求编号 ${requestId}` : "",
      ], response.status, requestId);
    }
    return body;
  }

  function createPrintPage(options) {
    const {
      printButton,
      retryButton,
      loadState,
      errorBox,
      content,
      render,
    } = options;
    let loadController = null;
    let loadGeneration = 0;

    function showLoading() {
      printButton.disabled = true;
      retryButton.hidden = true;
      retryButton.classList.add("hidden");
      errorBox.hidden = true;
      loadState.textContent = options.loadingMessage || "正在读取打印内容…";
      loadState.hidden = false;
      content.hidden = true;
    }

    function showError(message, allowRetry = true) {
      printButton.disabled = true;
      loadState.hidden = true;
      errorBox.textContent = message;
      errorBox.hidden = false;
      retryButton.hidden = !allowRetry;
      retryButton.classList.toggle("hidden", !allowRetry);
      content.hidden = true;
    }

    function showReady() {
      loadState.hidden = true;
      errorBox.hidden = true;
      retryButton.hidden = true;
      retryButton.classList.add("hidden");
      content.hidden = false;
      printButton.disabled = false;
    }

    async function load() {
      const id = String(options.id || "").trim();
      if (!/^\d+$/.test(id) || Number(id) <= 0) {
        showError(options.invalidMessage || "打印链接无效，请返回 ERP 重新打开。", false);
        return false;
      }
      if (loadController) loadController.abort();
      const controller = new AbortController();
      loadController = controller;
      const generation = ++loadGeneration;
      showLoading();
      try {
        const data = await requestPrintData(
          options.url(id),
          controller.signal,
          { auth: options.authMessage, failure: options.failureMessage },
        );
        if (generation !== loadGeneration || controller.signal.aborted) return false;
        try {
          render(data);
        } catch (error) {
          throw new Error(`打印内容无法显示：${error?.message || "页面渲染失败"}`);
        }
        showReady();
        return true;
      } catch (error) {
        if (generation !== loadGeneration || controller.signal.aborted || error?.name === "AbortError") {
          return false;
        }
        if (Number(error?.status) === 401 && typeof options.onUnauthorized === "function") {
          loadState.textContent = "登录已失效，正在返回 ERP…";
          options.onUnauthorized(error);
          return false;
        }
        const message = error instanceof TypeError
          ? "无法连接 ERP 服务，请确认服务正常后点击“重新读取”。"
          : (error?.message || `${options.failureMessage || "打印内容读取失败"}，请点击“重新读取”。`);
        showError(message, true);
        return false;
      } finally {
        if (generation === loadGeneration) loadController = null;
      }
    }

    printButton.addEventListener("click", () => {
      if (!printButton.disabled) (options.print || global.print.bind(global))();
    });
    retryButton.addEventListener("click", () => load());
    showLoading();
    return { load, showError };
  }

  global.TmPrintRecovery = Object.freeze({
    createPrintPage,
    parseErrorDetail,
    requestPrintData,
  });
})(window);
