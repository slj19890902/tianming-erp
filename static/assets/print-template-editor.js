(function (global) {
  "use strict";

  const DEFAULT_LABELS = Object.freeze({
    print: "打印 / 保存为 PDF",
    edit: "编辑打印内容",
    save: "保存",
    cancel: "取消",
    restore: "恢复系统原版",
    loading: "正在读取打印设置…",
    saved: "打印内容已保存",
    restored: "已恢复系统原版",
    conflict: "打印内容已在其他窗口更新，将刷新为最新版本。",
    unsavedPrint: "打印内容还有未保存的修改。继续打印将使用当前未保存内容，是否继续？",
    nativeUnsavedPrint: "浏览器原生打印无法由页面取消；本次将使用当前未保存内容。如不继续，请在打印对话框中取消。",
    unsavedLeave: "打印内容还有未保存的修改，确定离开吗？",
    restoreConfirm: "确定恢复系统原版打印内容吗？当前自定义内容将被清除。",
    savedApplyFailed: "打印内容已保存，但页面刷新失败。请重新打开本页面后再打印。",
    restoredApplyFailed: "系统原版已恢复，但页面刷新失败。请重新打开本页面后再打印。",
    loadedApplyFailed: "打印设置已载入，但页面刷新失败。请重新打开本页面。",
    cancelledApplyFailed: "修改已取消，但页面刷新失败。请重新打开本页面。",
  });

  function isElement(value) {
    return value && value.nodeType === 1;
  }

  function resolveElement(value, root) {
    if (!value) return null;
    if (typeof value === "string") return root.querySelector(value);
    return isElement(value) ? value : null;
  }

  function resolveElements(value, root) {
    if (!value) return [];
    if (typeof value === "string") return Array.from(root.querySelectorAll(value));
    if (isElement(value)) return [value];
    if (typeof value[Symbol.iterator] === "function") {
      return Array.from(value).filter(isElement);
    }
    return [];
  }

  function copyFields(fields) {
    const result = {};
    if (!fields || typeof fields !== "object" || Array.isArray(fields)) return result;
    Object.entries(fields).forEach(([key, value]) => {
      if (typeof key === "string" && value !== null && value !== undefined) {
        result[key] = String(value);
      }
    });
    return result;
  }

  function responseMessage(body, fallback) {
    if (!body || typeof body !== "object") return fallback;
    if (typeof body.detail === "string") return body.detail;
    if (body.detail && typeof body.detail.message === "string") return body.detail.message;
    if (typeof body.message === "string") return body.message;
    return fallback;
  }

  function makeButton(documentRef, action, textValue) {
    const button = documentRef.createElement("button");
    button.type = "button";
    button.dataset.printEditorAction = action;
    button.className = "print-template-editor-button print-template-editor-only";
    button.textContent = textValue;
    button.hidden = true;
    return button;
  }

  function installStyle(documentRef) {
    if (documentRef.getElementById("printTemplateEditorStyle")) return;
    const style = documentRef.createElement("style");
    style.id = "printTemplateEditorStyle";
    style.textContent = `
      .print-template-editor-button { margin-left: 8px; }
      .print-template-editor-status { margin-left: 10px; font-size: 12px; color: #4b5563; }
      [data-print-edit-key][contenteditable="true"] {
        outline: 2px dashed #2563eb;
        outline-offset: 3px;
        background: #eff6ff;
        cursor: text;
      }
      [data-print-edit-key][contenteditable="true"]:focus {
        outline-style: solid;
        background: #fff;
      }
      @media print {
        .print-template-editor-only { display: none !important; }
        [data-print-edit-key] { outline: 0 !important; background: transparent !important; }
      }
    `;
    documentRef.head.appendChild(style);
  }

  class PrintTemplateEditorInstance {
    constructor(options) {
      if (!options || !String(options.templateKey || "").trim()) {
        throw new Error("PrintTemplateEditor.create 需要 templateKey");
      }
      this.templateKey = String(options.templateKey).trim();
      this.root = options.root || global.document;
      this.document = this.root.ownerDocument || this.root;
      this.toolbar = resolveElement(options.toolbar, this.root);
      this.printButtons = resolveElements(
        options.printButton || "[data-print-action=\"print\"]",
        this.root,
      );
      this.labels = { ...DEFAULT_LABELS, ...(options.labels || {}) };
      this.onApplied = typeof options.onApplied === "function" ? options.onApplied : null;
      this.onBeforePrint = typeof options.onBeforePrint === "function" ? options.onBeforePrint : null;
      this.onPrint = typeof options.onPrint === "function" ? options.onPrint : null;
      this.handleEscape = options.handleEscape !== false;
      this.revision = 0;
      this.canManage = false;
      this.editing = false;
      this.dirty = false;
      this.defaults = {};
      this.savedFields = {};
      this.workingFields = {};
      this.fieldNodes = new Map();
      this._destroyed = false;
      this._buttonHandlers = [];

      installStyle(this.document);
      this._scanFields(true);
      this.savedFields = { ...this.defaults };
      this.workingFields = { ...this.defaults };
      this._buildToolbar();
      this._bindEvents();
      this.ready = this._initialize();
    }

    value() {
      return {
        template_key: this.templateKey,
        revision: this.revision,
        fields: { ...this.workingFields },
        can_manage: this.canManage,
        editing: this.editing,
        dirty: this.dirty,
      };
    }

    toolbars() {
      return {
        container: this.toolbar,
        print: this.printButtons[0] || null,
        printButtons: [...this.printButtons],
        edit: this.editButton,
        save: this.saveButton,
        cancel: this.cancelButton,
        restore: this.restoreButton,
        status: this.statusNode,
      };
    }

    setAfterApply(callback) {
      this.onApplied = typeof callback === "function" ? callback : null;
      return this;
    }

    async _initialize() {
      this._setStatus(this.labels.loading);
      try {
        await this.reapply("initial");
      } catch (error) {
        if (this._isApplyCallbackError(error)) {
          this._setStatus(this.labels.loadedApplyFailed, true);
          return this;
        }
        this.canManage = false;
        await this._acceptFields(this.defaults, "initial");
        this._setStatus(error.message || "打印设置暂不可用", true);
      }
      return this;
    }

    _nodesWithin(target) {
      const scope = target && (target.nodeType === 1 || target.nodeType === 9 || target.nodeType === 11)
        ? target
        : this.root;
      const nodes = [];
      if (scope.nodeType === 1 && scope.matches("[data-print-edit-key]")) nodes.push(scope);
      if (typeof scope.querySelectorAll === "function") {
        nodes.push(...scope.querySelectorAll("[data-print-edit-key]"));
      }
      return nodes;
    }

    _scanFields(captureDefaults, target = this.root) {
      const nodes = this._nodesWithin(target);
      const grouped = new Map();
      nodes.forEach((node) => {
        const key = String(node.dataset.printEditKey || "").trim();
        if (!key) return;
        if (!grouped.has(key)) grouped.set(key, []);
        grouped.get(key).push(node);
      });
      if (captureDefaults) {
        grouped.forEach((fieldNodes, key) => {
          if (Object.prototype.hasOwnProperty.call(this.defaults, key)) return;
          const segmented = fieldNodes.some((node) => node.dataset.printEditSegment === "true");
          this.defaults[key] = segmented
            ? fieldNodes.filter((node) => node.dataset.printEditSegment === "true").map((node) => node.textContent || "").join("")
            : fieldNodes[0]?.textContent || "";
        });
      }
      if (target === this.root) this.fieldNodes = grouped;
      return grouped;
    }

    _buildToolbar() {
      if (!this.toolbar) return;
      this.toolbar.dataset.printEditorToolbar = this.templateKey;
      this.editButton = makeButton(this.document, "edit", this.labels.edit);
      this.saveButton = makeButton(this.document, "save", this.labels.save);
      this.cancelButton = makeButton(this.document, "cancel", this.labels.cancel);
      this.restoreButton = makeButton(this.document, "restore", this.labels.restore);
      this.statusNode = this.document.createElement("span");
      this.statusNode.className = "print-template-editor-status print-template-editor-only";
      this.statusNode.setAttribute("role", "status");
      this.statusNode.setAttribute("aria-live", "polite");
      const inheritedClasses = this.printButtons[0]
        ? Array.from(this.printButtons[0].classList).filter((name) => name !== "danger")
        : [];
      [this.editButton, this.saveButton, this.cancelButton, this.restoreButton].forEach((button) => {
        inheritedClasses.forEach((name) => button.classList.add(name));
      });
      [this.editButton, this.saveButton, this.cancelButton, this.restoreButton, this.statusNode]
        .forEach((node) => this.toolbar.appendChild(node));
      this._updateToolbar();
    }

    _listen(element, eventName, handler, options) {
      if (!element) return;
      element.addEventListener(eventName, handler, options);
      this._buttonHandlers.push(() => element.removeEventListener(eventName, handler, options));
    }

    _bindEvents() {
      this._listen(this.editButton, "click", () => this.startEditing());
      this._listen(this.saveButton, "click", () => this.save());
      this._listen(this.cancelButton, "click", () => this.cancel());
      this._listen(this.restoreButton, "click", () => this.restore());
      this.printButtons.forEach((button) => {
        this._listen(button, "click", (event) => {
          event.preventDefault();
          event.stopImmediatePropagation();
          this.print();
        }, true);
      });
      this._listen(this.root, "input", (event) => this._onInput(event), true);
      this._listen(this.root, "paste", (event) => this._onPaste(event), true);
      this._listen(this.root, "keydown", (event) => this._onKeydown(event), true);
      this._beforeUnload = (event) => {
        if (!this.dirty) return undefined;
        event.preventDefault();
        event.returnValue = this.labels.unsavedLeave;
        return this.labels.unsavedLeave;
      };
      this._beforePrint = () => {
        try {
          const prepared = this._prepareForPrint("native");
          if (prepared && typeof prepared.catch === "function") {
            prepared.catch((error) => this._reportPrintPreparationFailure(error));
          }
        } catch (error) {
          this._reportPrintPreparationFailure(error);
        }
        if (this.dirty) this._setStatus(this.labels.nativeUnsavedPrint, true);
      };
      this._printShortcut = (event) => {
        if (!(event.ctrlKey || event.metaKey) || String(event.key || "").toLowerCase() !== "p") return;
        event.preventDefault();
        if (typeof event.stopImmediatePropagation === "function") event.stopImmediatePropagation();
        this.print();
      };
      global.addEventListener("beforeunload", this._beforeUnload);
      global.addEventListener("beforeprint", this._beforePrint);
      global.addEventListener("keydown", this._printShortcut, true);
    }

    _editableTarget(target) {
      if (!this.editing || !target || typeof target.closest !== "function") return null;
      const node = target.closest("[data-print-edit-key]");
      if (!node || node.getAttribute("contenteditable") !== "true") return null;
      if (this.root.nodeType === 1 && !this.root.contains(node)) return null;
      return node;
    }

    _onInput(event) {
      const node = this._editableTarget(event.target);
      if (!node) return;
      const key = node.dataset.printEditKey;
      const nodes = this.fieldNodes.get(key) || [];
      const segmented = node.dataset.printEditSegment === "true";
      const value = segmented
        ? nodes.filter((peer) => peer.dataset.printEditSegment === "true").map((peer) => peer.textContent || "").join("")
        : node.textContent || "";
      this.workingFields[key] = value;
      nodes.forEach((peer) => {
        if (segmented || peer.dataset.printEditSegment === "true") return;
        if (peer !== node && peer.textContent !== value) peer.textContent = value;
      });
      this._refreshDirty();
    }

    _onPaste(event) {
      const node = this._editableTarget(event.target);
      if (!node) return;
      event.preventDefault();
      const plainText = (event.clipboardData || global.clipboardData)?.getData("text/plain") || "";
      const selection = global.getSelection ? global.getSelection() : null;
      if (!selection || !selection.rangeCount || !node.contains(selection.getRangeAt(0).commonAncestorContainer)) {
        node.textContent = `${node.textContent || ""}${plainText}`;
      } else {
        const range = selection.getRangeAt(0);
        range.deleteContents();
        const textNode = this.document.createTextNode(plainText);
        range.insertNode(textNode);
        range.setStartAfter(textNode);
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
      }
      this._onInput({ target: node });
    }

    _onKeydown(event) {
      const node = this._editableTarget(event.target);
      if (!node) return;
      if (event.key === "Escape" && this.handleEscape) {
        event.preventDefault();
        this.cancel();
      } else if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        this.save();
      }
    }

    _refreshDirty() {
      const keys = new Set([...Object.keys(this.savedFields), ...Object.keys(this.workingFields)]);
      this.dirty = Array.from(keys).some(
        (key) => String(this.savedFields[key] ?? "") !== String(this.workingFields[key] ?? ""),
      );
      this._updateToolbar();
    }

    applyTo(target = this.root) {
      const grouped = this._scanFields(false, target);
      grouped.forEach((nodes, key) => {
        const segmented = nodes.some((node) => node.dataset.printEditSegment === "true");
        if (!Object.prototype.hasOwnProperty.call(this.defaults, key)) {
          this.defaults[key] = segmented
            ? nodes.filter((node) => node.dataset.printEditSegment === "true").map((node) => node.textContent || "").join("")
            : nodes[0]?.textContent || "";
        }
        if (!Object.prototype.hasOwnProperty.call(this.workingFields, key)) {
          this.savedFields[key] = String(this.defaults[key] ?? "");
          this.workingFields[key] = this.savedFields[key];
        }
        const value = String(this.workingFields[key] ?? this.defaults[key] ?? "");
        if (segmented) return;
        nodes.forEach((node) => {
          node.textContent = value;
        });
      });
      if (target === this.root) this.fieldNodes = grouped;
      this._setEditable(this.editing);
      return target;
    }

    _renderFields() {
      this.applyTo(this.root);
    }

    async _notifyApplied(reason) {
      let callbackError = null;
      try {
        if (this.onApplied) await this.onApplied(this.value(), reason);
      } catch (error) {
        callbackError = error;
      }
      if (this._destroyed) return;
      this._scanFields(false);
      this._renderFields();
      this._setEditable(this.editing);
      if (callbackError) {
        const error = new Error(callbackError.message || "页面刷新失败");
        error.printTemplateApplyCallbackFailed = true;
        error.cause = callbackError;
        throw error;
      }
    }

    async _acceptFields(fields, reason, response = null) {
      const incoming = copyFields(fields);
      if (response) {
        if (response.template_key && response.template_key !== this.templateKey) {
          throw new Error("打印设置返回了错误的模板 key");
        }
        this.revision = Number.isFinite(Number(response.revision)) ? Number(response.revision) : 0;
        this.canManage = response.can_manage === true;
      }
      const effective = { ...this.defaults, ...incoming };
      this.savedFields = { ...effective };
      this.workingFields = { ...effective };
      this.editing = false;
      this.dirty = false;
      this._renderFields();
      this._setEditable(false);
      this._updateToolbar();
      await this._notifyApplied(reason);
      return this.value();
    }

    async _acceptResponse(response, reason) {
      if (!response || typeof response !== "object" || Array.isArray(response)) {
        throw new Error("打印设置响应格式无效");
      }
      return this._acceptFields(response.fields, reason, response);
    }

    async apply(fieldsOrResponse, reason = "apply") {
      if (
        fieldsOrResponse
        && typeof fieldsOrResponse === "object"
        && !Array.isArray(fieldsOrResponse)
        && Object.prototype.hasOwnProperty.call(fieldsOrResponse, "fields")
      ) {
        return this._acceptResponse(fieldsOrResponse, reason);
      }
      return this._acceptFields(fieldsOrResponse, reason);
    }

    async _request(method, suffix, body) {
      const response = await global.fetch(
        `/api/system/print-template-settings/${encodeURIComponent(this.templateKey)}${suffix || ""}`,
        {
          method,
          credentials: "same-origin",
          headers: body ? { "Content-Type": "application/json" } : undefined,
          body: body ? JSON.stringify(body) : undefined,
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(responseMessage(payload, `请求失败（HTTP ${response.status}）`));
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return payload;
    }

    async reapply(reason = "reapply") {
      const response = await this._request("GET", "", null);
      const result = await this._acceptResponse(response, reason);
      this._setStatus("");
      return result;
    }

    startEditing() {
      if (!this.canManage) return false;
      this.editing = true;
      this.workingFields = { ...this.savedFields };
      this.dirty = false;
      this._renderFields();
      this._setEditable(true);
      this._updateToolbar();
      const first = this.root.querySelector("[data-print-edit-key][contenteditable=\"true\"]");
      if (first && typeof first.focus === "function") first.focus();
      return true;
    }

    _setEditable(enabled) {
      this._scanFields(false);
      this.fieldNodes.forEach((nodes, key) => {
        nodes.forEach((node) => {
          if (enabled && this.canManage) {
            node.setAttribute("contenteditable", "true");
            node.setAttribute("spellcheck", "false");
            node.setAttribute("role", "textbox");
            node.setAttribute("aria-label", node.dataset.printEditLabel || key);
          } else {
            node.removeAttribute("contenteditable");
            node.removeAttribute("spellcheck");
            node.removeAttribute("role");
            node.removeAttribute("aria-label");
          }
        });
      });
    }

    async _handleConflict(error) {
      if (error.status !== 409) return false;
      global.alert(this.labels.conflict);
      try {
        await this.reapply("reapply");
      } catch (reapplyError) {
        if (!this._isApplyCallbackError(reapplyError)) throw reapplyError;
        this._setStatus(this.labels.loadedApplyFailed, true);
        global.alert(this.labels.loadedApplyFailed);
      }
      return true;
    }

    _isApplyCallbackError(error) {
      return error?.printTemplateApplyCallbackFailed === true;
    }

    async save() {
      if (!this.canManage || !this.editing) return false;
      try {
        const response = await this._request("PUT", "", {
          expected_revision: this.revision,
          fields: { ...this.workingFields },
        });
        try {
          await this._acceptResponse(response, "save");
        } catch (error) {
          if (!this._isApplyCallbackError(error)) throw error;
          this._setStatus(this.labels.savedApplyFailed, true);
          global.alert(this.labels.savedApplyFailed);
          return true;
        }
        this._setStatus(this.labels.saved);
        return true;
      } catch (error) {
        if (await this._handleConflict(error)) return false;
        this._setStatus(error.message, true);
        global.alert(error.message);
        return false;
      }
    }

    async cancel() {
      this.workingFields = { ...this.savedFields };
      this.editing = false;
      this.dirty = false;
      this._renderFields();
      this._setEditable(false);
      this._updateToolbar();
      try {
        await this._notifyApplied("cancel");
      } catch (error) {
        if (!this._isApplyCallbackError(error)) throw error;
        this._setStatus(this.labels.cancelledApplyFailed, true);
        global.alert(this.labels.cancelledApplyFailed);
        return true;
      }
      this._setStatus("");
      return true;
    }

    async restore() {
      if (!this.canManage) return false;
      if (!global.confirm(this.labels.restoreConfirm)) return false;
      try {
        const response = await this._request("POST", "/restore", {
          expected_revision: this.revision,
        });
        try {
          await this._acceptResponse(response, "restore");
        } catch (error) {
          if (!this._isApplyCallbackError(error)) throw error;
          this._setStatus(this.labels.restoredApplyFailed, true);
          global.alert(this.labels.restoredApplyFailed);
          return true;
        }
        this._setStatus(this.labels.restored);
        return true;
      } catch (error) {
        if (await this._handleConflict(error)) return false;
        this._setStatus(error.message, true);
        global.alert(error.message);
        return false;
      }
    }

    beforePrint() {
      if (!this.dirty) return true;
      return global.confirm(this.labels.unsavedPrint);
    }

    _prepareForPrint(reason) {
      if (!this.onBeforePrint) return null;
      return this.onBeforePrint(this.value(), reason);
    }

    _reportPrintPreparationFailure(error) {
      const message = error?.message || "打印版式更新失败，请取消本次打印并重试";
      this._setStatus(message, true);
    }

    async print() {
      if (!this.beforePrint()) return false;
      try {
        await this._prepareForPrint("button");
        if (this.onPrint) await this.onPrint(this.value());
        else global.print();
        return true;
      } catch (error) {
        this._setStatus(error.message || "打印失败", true);
        global.alert(error.message || "打印失败");
        return false;
      }
    }

    reset() {
      return this.restore();
    }

    _setStatus(message, isError) {
      if (!this.statusNode) return;
      this.statusNode.textContent = message || "";
      this.statusNode.style.color = isError ? "#b91c1c" : "#4b5563";
    }

    _updateToolbar() {
      if (!this.toolbar) return;
      const managed = this.canManage === true;
      if (this.editButton) this.editButton.hidden = !managed || this.editing;
      if (this.restoreButton) this.restoreButton.hidden = !managed || this.editing;
      if (this.saveButton) {
        this.saveButton.hidden = !managed || !this.editing;
        this.saveButton.disabled = !this.dirty;
      }
      if (this.cancelButton) this.cancelButton.hidden = !managed || !this.editing;
      if (this.statusNode) this.statusNode.hidden = !managed;
    }

    destroy() {
      this._destroyed = true;
      this._buttonHandlers.splice(0).forEach((remove) => remove());
      global.removeEventListener("beforeunload", this._beforeUnload);
      global.removeEventListener("beforeprint", this._beforePrint);
      global.removeEventListener("keydown", this._printShortcut, true);
      this._setEditable(false);
    }
  }

  global.PrintTemplateEditor = Object.freeze({
    create(options) {
      return new PrintTemplateEditorInstance(options);
    },
  });
})(window);
