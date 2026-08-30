import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { moldRackDraftWorkflowState } from "../src/moldRackDraftWorkflow.mjs";

const appSource = readFileSync(
  new URL("../src/WarehouseTwinApp.tsx", import.meta.url),
  "utf8"
);

test("mold rack draft buttons unlock in save validate publish order", () => {
  const editing = moldRackDraftWorkflowState({
    busy: false,
    hasDraft: false,
    status: "none",
    hasUnsavedInput: true
  });
  assert.equal(editing.save.disabled, false);
  assert.equal(editing.validate.disabled, true);
  assert.equal(editing.publish.disabled, true);
  assert.match(editing.validate.title, /第①步/);
  assert.match(editing.publish.title, /第①步/);

  const saved = moldRackDraftWorkflowState({
    busy: false,
    hasDraft: true,
    status: "draft",
    hasUnsavedInput: false
  });
  assert.equal(saved.save.disabled, false);
  assert.equal(saved.validate.disabled, false);
  assert.equal(saved.publish.disabled, true);
  assert.match(saved.publish.title, /第②步/);

  const validated = moldRackDraftWorkflowState({
    busy: false,
    hasDraft: true,
    status: "validated",
    hasUnsavedInput: false
  });
  assert.equal(validated.validate.disabled, false);
  assert.equal(validated.publish.disabled, false);
  assert.match(validated.publish.title, /只发布当前楼层/);
});

test("unsaved input failure and busy state keep later steps blocked", () => {
  const editedAfterValidation = moldRackDraftWorkflowState({
    busy: false,
    hasDraft: true,
    status: "validated",
    hasUnsavedInput: true
  });
  assert.equal(editedAfterValidation.save.disabled, false);
  assert.equal(editedAfterValidation.validate.disabled, true);
  assert.equal(editedAfterValidation.publish.disabled, true);
  assert.match(editedAfterValidation.validate.title, /第①步/);
  assert.match(editedAfterValidation.publish.title, /第①步/);

  const busy = moldRackDraftWorkflowState({
    busy: true,
    hasDraft: true,
    status: "validated",
    hasUnsavedInput: false
  });
  assert.equal(busy.save.disabled, true);
  assert.equal(busy.validate.disabled, true);
  assert.equal(busy.publish.disabled, true);
  assert.match(busy.save.title, /处理中/);
});

test("the live mold rack buttons use the tested workflow state", () => {
  assert.match(appSource, /const selectedMoldRackDraftWorkflow = moldRackDraftWorkflowState\(\{/);
  assert.match(
    appSource,
    /disabled=\{selectedMoldRackDraftWorkflow\.save\.disabled\}/
  );
  assert.match(
    appSource,
    /disabled=\{selectedMoldRackDraftWorkflow\.validate\.disabled\}/
  );
  assert.match(
    appSource,
    /disabled=\{selectedMoldRackDraftWorkflow\.publish\.disabled\}/
  );
  assert.match(
    appSource,
    /const saveRackDraftImmediately = async[\s\S]*rememberServerDraft\(response\.revision\);[\s\S]*delete next\[rackId\]/
  );
  assert.match(
    appSource,
    /status: result\.status,[\s\S]*blockers: result\.blockers,[\s\S]*warnings: result\.warnings/
  );
});
