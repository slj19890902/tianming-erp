from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")


def test_pdf_order_save_template_exposes_persistent_per_file_status_and_retry() -> None:
    for expected in (
        "importDraftSaveStatusClass(draft)",
        "importDraftSaveStatusText(draft)",
        "draft._save_status === 'failed'",
        "retryFailedImportDraft(draft)",
        "successfulImportDraftCount",
        "failedImportDraftCount",
        "保存/重试已确认草稿",
        "正在保存",
        "保存成功",
        "保存失败，可重试",
        'class="pdf-import-draft-fields"',
        ':disabled="isImportDraftLocked(draft)"',
        'placeholder="手动选择客户" :disabled="isImportDraftLocked(draft)"',
        'placeholder="选择实际材质" :disabled="isImportDraftLocked(draft)"',
    ):
        assert expected in INDEX

    save_block = INDEX[
        INDEX.index("async saveConfirmedImportDrafts()") :
        INDEX.index("openOrderEditor(group)", INDEX.index("async saveConfirmedImportDrafts()"))
    ]
    assert '!["saving","success"].includes(draft._save_status)' in save_block
    assert 'draft._save_status = "saving"' in save_block
    assert 'draft._save_status = "success"' in save_block
    assert 'draft._save_status = "failed"' in save_block
    assert "draft.confirmed = false" in save_block
    assert "draft._save_message = message" in save_block


def test_pdf_order_save_error_matrix_and_partial_retry_execute_real_vue_methods() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the PDF order frontend behavior test"

    harness = r"""
const fs = require("fs");
const vm = require("vm");

const indexPath = process.argv[2];
const html = fs.readFileSync(indexPath, "utf8");
const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1])
  .filter(source => source.trim());
if (scripts.length !== 1) {
  throw new Error(`Expected one inline application script, found ${scripts.length}`);
}

const sandbox = {
  axios: {
    defaults: {},
    interceptors: { response: { use() {} } },
  },
  Vue: {
    createApp(definition) {
      sandbox.definition = definition;
      return {
        component() { return this; },
        mount() { return this; },
      };
    },
  },
  localStorage: {
    getItem() { return ""; },
    setItem() {},
    removeItem() {},
  },
  window: {},
  console,
  URLSearchParams,
  setTimeout,
  clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(scripts[0], sandbox);

const methods = sandbox.definition.methods;
const computed = sandbox.definition.computed;
const context = { ...methods };
const forbiddenAttribution = "请先检查客户、产品和数量";
const normalizedMessages = [];

function normalize(error) {
  const message = methods.normalizeOrderSaveError.call(context, error);
  normalizedMessages.push(message);
  return message;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const stringDetail = "客户单号已存在，请核对后重试";
assert(
  normalize({ response: { status: 400, data: { detail: stringDetail } } }) === stringDetail,
  "String detail was not preserved",
);

const objectMessage = "订单保存条件已变化";
const objectCode = "ORDER_STATE_CHANGED";
const objectResult = normalize({
  response: {
    status: 400,
    data: { detail: { message: objectMessage, code: objectCode } },
  },
});
assert(objectResult.includes(objectMessage), "Object detail.message was lost");
assert(objectResult.includes(objectCode), "Safe object detail.code was lost");

const unsafeCodeResult = normalize({
  response: {
    status: 400,
    data: {
      detail: {
        message: "后端拒绝了订单保存",
        code: "<script>alert(1)</script>",
      },
    },
  },
});
assert(unsafeCodeResult === "后端拒绝了订单保存", "Unsafe error code was rendered");

const validationResult = normalize({
  response: {
    status: 422,
    data: {
      detail: [
        { loc: ["body", "items", 0, "product_id"], msg: "Field required" },
        { loc: ["body", "items", 1, "product_id"], msg: "Field required" },
        { loc: ["body", "items", 2, "product_id"], msg: "Field required" },
        { loc: ["body", "items", 3, "product_id"], msg: "Field required" },
        { loc: ["body", "items", 4, "product_id"], msg: "Field required" },
        { loc: ["body", "items", 5, "product_id"], msg: "Field required" },
        { loc: ["body", "items", 6, "product_id"], msg: "Field required" },
      ],
    },
  },
});
for (let line = 1; line <= 7; line += 1) {
  assert(validationResult.includes(`第${line}条产品不能为空`), validationResult);
}
assert(!validationResult.includes("items.6.product_id"), validationResult);

const conflictMessage = "PDF 预览确认已过期，请重新预览";
const conflictCode = "PDF_PREVIEW_TOKEN_STALE";
const conflictResult = normalize({
  response: {
    status: 409,
    data: { detail: { message: conflictMessage, code: conflictCode } },
  },
});
assert(conflictResult.includes(conflictMessage), "409 message was lost");
assert(conflictResult.includes(conflictCode), "409 code was lost");

const json500Result = normalize({
  response: {
    status: 500,
    data: {
      detail: {
        message: "订单创建阶段发生内部错误",
        code: "ORDER_SAVE_INTERNAL_ERROR",
        request_id: "req-json-500",
      },
    },
  },
});
assert(json500Result.includes("HTTP 500"), json500Result);
assert(json500Result.includes("req-json-500"), json500Result);
assert(json500Result.includes("ORDER_SAVE_INTERNAL_ERROR"), json500Result);

const text500Result = normalize({
  response: {
    status: 500,
    data: "Internal Server Error",
    headers: { "x-request-id": "req-text-500" },
  },
});
assert(text500Result.includes("HTTP 500"), text500Result);
assert(text500Result.includes("req-text-500"), text500Result);
assert(text500Result.includes("服务器内部错误"), text500Result);
assert(!text500Result.includes("Internal Server Error"), text500Result);

const empty500Result = normalize({
  response: {
    status: 500,
    data: "",
    headers: {
      get(name) {
        return name.toLowerCase() === "x-request-id" ? "req-empty-500" : null;
      },
    },
  },
});
assert(empty500Result.includes("HTTP 500"), empty500Result);
assert(empty500Result.includes("req-empty-500"), empty500Result);
assert(empty500Result.includes("服务器内部错误"), empty500Result);

const networkResult = normalize({
  isAxiosError: true,
  code: "ERR_NETWORK",
  message: "Network Error",
});
assert(networkResult.includes("网络连接失败"), networkResult);

const timeoutResult = normalize({
  isAxiosError: true,
  code: "ECONNABORTED",
  message: "timeout of 30000ms exceeded",
});
assert(timeoutResult.includes("请求超时"), timeoutResult);

for (const message of normalizedMessages) {
  assert(!message.includes(forbiddenAttribution), `Unsupported field attribution: ${message}`);
}

let failSecondOnce = true;
const postCalls = [];
sandbox.axios.post = async (url, payload) => {
  assert(url === "/api/orders", `Unexpected URL: ${url}`);
  const sourceName = payload.remark.split("：").pop();
  postCalls.push(sourceName);
  if (sourceName === "failed.pdf" && failSecondOnce) {
    failSecondOnce = false;
    throw {
      response: {
        status: 409,
        data: {
          detail: {
            message: conflictMessage,
            code: conflictCode,
          },
        },
      },
    };
  }
  return { data: { id: postCalls.length } };
};

const makeItem = () => ({
  matched_product_id: 1,
  is_new_product: false,
  material_candidates: [],
  quantity: 1,
  unit_price: "1.00",
  client_line_id: "line-id",
  product_name: "测试纸箱",
});
const baseDraft = {
  confirmed: true,
  _save_status: "idle",
  _save_message: "",
  matched_customer_id: 1,
  customer_po: "PO-P0-FRONTEND",
  order_date: "2026-07-27",
  delivery_date: null,
  integrity_check: { integrity_status: "passed" },
};
const successDraft = {
  ...baseDraft,
  source_name: "success.pdf",
  items: [makeItem()],
};
const failedDraft = {
  ...baseDraft,
  source_name: "failed.pdf",
  items: [makeItem()],
};
const toasts = [];
Object.assign(context, {
  orderImportBatch: { retryDraft: null },
  orderImportDrafts: [successDraft, failedDraft],
  loading: false,
  modal: { type: "orderPdfImport" },
  canConfirmImportDraft() { return true; },
  inventoryDecisionRequired() { return ""; },
  buildReservationPlan() { return {}; },
  async loadOrders() {},
  async loadKpi() {},
  showToast(message, error = false) { toasts.push({ message, error }); },
  async syncProductFieldsVersioned() { return true; },
});

(async () => {
  const firstSave = methods.saveConfirmedImportDrafts.call(context);
  assert(successDraft._save_status === "saving", "First draft did not expose saving state");
  await firstSave;

  assert(successDraft._save_status === "success", "Successful draft status was not retained");
  assert(successDraft.confirmed === false, "Successful draft was not locked");
  assert(successDraft._save_message.includes("保存成功"), successDraft._save_message);
  assert(failedDraft._save_status === "failed", "Failed draft status was not retained");
  assert(failedDraft.confirmed === true, "Failed draft was not kept retryable");
  assert(failedDraft._save_message.includes(conflictMessage), failedDraft._save_message);
  assert(failedDraft._save_message.includes(conflictCode), failedDraft._save_message);
  assert(computed.successfulImportDraftCount.call(context) === 1, "Success count is wrong");
  assert(computed.failedImportDraftCount.call(context) === 1, "Failure count is wrong");
  assert(computed.confirmedImportDraftCount.call(context) === 1, "Retryable count is wrong");
  assert(context.modal !== null, "Partial failure incorrectly closed the modal");

  await methods.retryFailedImportDraft.call(context, failedDraft);

  assert(failedDraft._save_status === "success", "Failed draft did not succeed on retry");
  assert(failedDraft.confirmed === false, "Retried draft was not locked after success");
  assert(context.orderImportBatch.retryDraft === null, "Retry selector was not cleared");
  assert(postCalls.filter(name => name === "success.pdf").length === 1, "Successful draft was submitted twice");
  assert(postCalls.filter(name => name === "failed.pdf").length === 2, "Only failed draft should be retried");

  await methods.saveConfirmedImportDrafts.call(context);
  assert(postCalls.filter(name => name === "success.pdf").length === 1, "Locked success was resubmitted");
  assert(postCalls.filter(name => name === "failed.pdf").length === 2, "Locked retry success was resubmitted");

  const lockedItemCount = successDraft.items.length;
  await methods.rematchImportDraft.call(context, 0);
  methods.removeImportDraftItem.call(context, successDraft, successDraft.items[0]);
  methods.invalidateImportDraftConfirmation.call(context, successDraft);
  assert(postCalls.length === 3, "Locked success triggered another network request");
  assert(successDraft._save_status === "success", "Locked success status was reset");
  assert(successDraft.items.length === lockedItemCount, "Locked success remained editable");

  process.stdout.write(JSON.stringify({
    normalizedCaseCount: normalizedMessages.length,
    firstSuccessAttempts: postCalls.filter(name => name === "success.pdf").length,
    failedDraftAttempts: postCalls.filter(name => name === "failed.pdf").length,
    finalStatuses: [successDraft._save_status, failedDraft._save_status],
    toastCount: toasts.length,
  }));
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""

    result = subprocess.run(
        [node, "-", str(INDEX_PATH)],
        input=harness,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["normalizedCaseCount"] == 10
    assert summary["firstSuccessAttempts"] == 1
    assert summary["failedDraftAttempts"] == 2
    assert summary["finalStatuses"] == ["success", "success"]
