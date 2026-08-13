import assert from "node:assert/strict";
import test from "node:test";

import {
  clearFormalAreaOptions,
  formalAreaOptionsEffectEnabled,
  stableTwinFeatures
} from "../src/formalAreaOptions.mjs";

test("initial empty layout keeps stable references and never enables the area request", () => {
  const firstFeatures = stableTwinFeatures(null);
  const secondFeatures = stableTwinFeatures(undefined);
  const currentOptions = [];

  assert.equal(firstFeatures, secondFeatures);
  assert.equal(clearFormalAreaOptions(currentOptions), currentOptions);
  assert.equal(formalAreaOptionsEffectEnabled({
    canEditLocations: true,
    locationEditMode: true,
    areaPolicyEditMode: true,
    hasSelectedFeature: false,
    formalAreaId: null
  }), false);

  // A second render with the same empty inputs has neither a new dependency
  // reference nor a state transition that could schedule another request.
  assert.equal(stableTwinFeatures(null), firstFeatures);
  assert.equal(clearFormalAreaOptions(currentOptions), currentOptions);
});

test("the request is enabled only for an editable, selected, unbound feature", () => {
  const base = {
    canEditLocations: true,
    locationEditMode: true,
    areaPolicyEditMode: true,
    hasSelectedFeature: true,
    formalAreaId: null
  };

  assert.equal(formalAreaOptionsEffectEnabled(base), true);
  assert.equal(formalAreaOptionsEffectEnabled({ ...base, formalAreaId: 28 }), false);
  assert.equal(formalAreaOptionsEffectEnabled({ ...base, areaPolicyEditMode: false }), false);
});

test("clearing non-empty options changes state once and then preserves identity", () => {
  const cleared = clearFormalAreaOptions([{ id: 28 }]);
  assert.deepEqual(cleared, []);
  assert.equal(clearFormalAreaOptions(cleared), cleared);
});
