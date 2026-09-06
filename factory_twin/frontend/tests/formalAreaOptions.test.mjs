import assert from "node:assert/strict";
import test from "node:test";

import {
  clearFormalAreaOptions,
  formalAreaOptionsEffectEnabled,
  filterPlanningPublishedFeatures,
  removeZoneHierarchy,
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

test("planning published base omits a zone removed from the active draft", () => {
  const published = {
    features: [
      { id: "zone-old", feature_kind: "zone" },
      { id: "zone-keep", feature_kind: "zone" },
      { id: "aisle-old", feature_kind: "aisle" }
    ]
  };
  const draft = { features: [{ id: "zone-keep", feature_kind: "zone" }] };
  assert.deepEqual(filterPlanningPublishedFeatures(published, draft).map((row) => row.id), ["zone-keep"]);
});

test("removing a zone also removes its stale rack and pallet overlays", () => {
  const layout = {
    features: [
      { id: "zone-old", feature_code: "ZONE-E2", erp_area_code: "E2" },
      { id: "zone-keep", feature_code: "ZONE-E3", erp_area_code: "E3" }
    ],
    racks: [
      { id: "rack-old", area_feature_id: "zone-old", area_code: "E2" },
      { id: "rack-keep", area_feature_id: "zone-keep", area_code: "E3" }
    ],
    pallets: [
      { id: "slot-old", zone_id: "zone-old", zone_code: "E2" },
      { id: "slot-keep", zone_id: "zone-keep", zone_code: "E3" }
    ]
  };
  const result = removeZoneHierarchy(layout, "zone-old", "E2");
  assert.deepEqual(result.features.map((row) => row.id), ["zone-keep"]);
  assert.deepEqual(result.racks.map((row) => row.id), ["rack-keep"]);
  assert.deepEqual(result.pallets.map((row) => row.id), ["slot-keep"]);
  assert.equal(layout.features.length, 2);
});
